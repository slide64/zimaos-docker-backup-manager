import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path, PurePosixPath

import docker
import yaml

APP_VERSION = '0.1.7'
DATA_DIR = Path('/data')
DB_PATH = DATA_DIR / 'manager.db'
OPERATION_LOCK = DATA_DIR / 'operation.lock'
LOG_DIR = DATA_DIR / 'logs'
PROGRESS_PATH = DATA_DIR / 'progress.json'
DEFAULT_DESTINATION = '/media/zimaos-backup-docker'
SELF_NAME = 'zimaos-docker-backup-manager'
TIMESTAMP_RE = re.compile(r'^\d{8}-\d{6}$')

_messages = []


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def settings():
    with db() as conn:
        return {r['key']: r['value'] for r in conn.execute('SELECT key,value FROM settings')}


def translate_host_path(host_path: str) -> Path:
    p = host_path.rstrip('/') or '/'
    mappings = [
        ('/media', '/host-media'),
        ('/mnt', '/host-mnt'),
        ('/DATA', '/host-data'),
        ('/var/lib/casaos/apps', '/host-compose'),
    ]
    for host_prefix, container_prefix in mappings:
        if p == host_prefix or p.startswith(host_prefix + '/'):
            return Path(container_prefix + p[len(host_prefix):])
    if p == '/var/run/docker.sock':
        return Path('/var/run/docker.sock')
    return Path(p)


def destination_path(host_destination: str) -> Path:
    if not host_destination.startswith(('/media/', '/mnt/', '/DATA/')):
        raise RuntimeError('destination invalide')
    return translate_host_path(host_destination)


def update_progress(percent, phase, message, status='running', container=None, operation='restore', run_id=None, log=None):
    global _messages
    if message:
        _messages.append(message)
        _messages = _messages[-14:]
        if log is not None:
            log.write(message + '\n')
            log.flush()
    payload = {
        'status': status,
        'kind': operation,
        'percent': max(0, min(100, int(percent))),
        'phase': phase,
        'message': message,
        'container': container,
        'restore_id': run_id,
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'messages': list(_messages),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    os.replace(tmp, PROGRESS_PATH)


def safe_name(name: str):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('._') or 'container'


def load_json(path: Path):
    return json.loads(path.read_text())


def run_rsync(src: Path, dst: Path, log, excludes=None):
    if not src.exists():
        return
    dst.mkdir(parents=True, exist_ok=True)
    cmd = ['rsync', '-aHAX', '--numeric-ids', '--stats']
    for pattern in excludes or []:
        cmd += ['--exclude', pattern]
    cmd += [str(src).rstrip('/') + '/', str(dst).rstrip('/') + '/']
    log.write('$ ' + ' '.join(cmd) + '\n')
    log.flush()
    proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f'rsync a échoué ({proc.returncode}) : {src}')


def snapshot_for_name(base: Path, snapshot_name: str) -> Path:
    if not TIMESTAMP_RE.match(snapshot_name):
        raise RuntimeError('nom de sauvegarde invalide')
    snap = base / snapshot_name
    if not snap.is_dir() or not (snap / 'manifest.json').is_file():
        raise RuntimeError('sauvegarde introuvable ou incomplète')
    return snap


def selected_manifest_entries(manifest, names):
    by_name = {c.get('name'): c for c in manifest.get('containers', []) if c.get('name')}
    missing = sorted(set(names) - set(by_name))
    if missing:
        raise RuntimeError('conteneur(s) absent(s) de la sauvegarde : ' + ', '.join(missing))
    return [by_name[n] for n in names]


def stop_existing(client, names, log, run_id):
    stopped = []
    for idx, name in enumerate(names, 1):
        if name == SELF_NAME:
            continue
        try:
            c = client.containers.get(name)
        except docker.errors.NotFound:
            continue
        c.reload()
        if c.status == 'running':
            update_progress(8, 'Préparation', f'Arrêt du conteneur existant : {name}', container=name, run_id=run_id, log=log)
            c.stop(timeout=30)
            stopped.append(name)
    return stopped


def restore_appdata_and_compose(snapshot: Path, entries, log, run_id, start_pct=12, end_pct=50):
    total = max(1, len(entries))
    for i, entry in enumerate(entries, 1):
        name = entry['name']
        cdir = snapshot / 'containers' / entry.get('folder', safe_name(name))
        pct = start_pct + int((i - 1) / total * (end_pct - start_pct))
        update_progress(pct, 'Restauration des fichiers', f'[{i}/{total}] {name} : AppData', container=name, run_id=run_id, log=log)
        appdata = cdir / 'appdata'
        if appdata.is_dir():
            run_rsync(appdata, Path('/host-appdata'), log, excludes=['zimaos-docker-backup-manager/'])

        compose_zimaos = cdir / 'compose' / 'zimaos'
        if compose_zimaos.is_dir():
            update_progress(pct + 1, 'Restauration des fichiers', f'[{i}/{total}] {name} : Compose ZimaOS', container=name, run_id=run_id, log=log)
            run_rsync(compose_zimaos, Path('/host-compose'), log)


def restore_volumes(client, snapshot: Path, entries, log, run_id, start_pct=50, end_pct=68):
    wanted = sorted({v for e in entries for v in e.get('named_volumes', []) if v})
    if not wanted:
        update_progress(end_pct, 'Volumes Docker', 'Aucun volume Docker nommé à restaurer.', run_id=run_id, log=log)
        return

    volume_inventory = {}
    inv_path = snapshot / 'inventaire' / 'volumes.json'
    if inv_path.is_file():
        try:
            volume_inventory = {v.get('Name'): v for v in load_json(inv_path) if v.get('Name')}
        except Exception:
            volume_inventory = {}

    total = len(wanted)
    for i, name in enumerate(wanted, 1):
        pct = start_pct + int(i / total * (end_pct - start_pct))
        update_progress(pct, 'Volumes Docker', f'[{i}/{total}] {name}', run_id=run_id, log=log)
        src = snapshot / 'shared-volumes' / name / '_data'
        if not src.is_dir():
            log.write(f'ATTENTION : données du volume absentes : {name}\n')
            continue
        meta = volume_inventory.get(name) or {}
        driver = meta.get('Driver') or 'local'
        try:
            volume = client.volumes.get(name)
        except docker.errors.NotFound:
            volume = client.volumes.create(
                name=name,
                driver=driver,
                driver_opts=meta.get('Options') or None,
                labels=meta.get('Labels') or None,
            )
        if driver != 'local':
            log.write(f'ATTENTION : volume {name} utilise le driver {driver}; données brutes non recopiées automatiquement.\n')
            continue
        info = client.api.inspect_volume(name)
        mountpoint = Path(info.get('Mountpoint') or '')
        if not str(mountpoint):
            raise RuntimeError(f'point de montage Docker introuvable pour le volume {name}')
        # Le Docker root hôte est monté dans /host-docker-root. On traduit son Mountpoint.
        docker_root_host = Path(os.environ.get('DOCKER_ROOT_DIR', '/var/lib/docker'))
        try:
            rel = mountpoint.relative_to(docker_root_host)
        except ValueError:
            rel = Path('volumes') / name / '_data'
        target = Path('/host-docker-root') / rel
        run_rsync(src, target, log)


def restore_files(client, snapshot: Path, entries, log, run_id):
    restore_appdata_and_compose(snapshot, entries, log, run_id)
    restore_volumes(client, snapshot, entries, log, run_id)


def network_create_kwargs(attrs):
    ipam_attrs = attrs.get('IPAM') or {}
    pools = []
    for cfg in ipam_attrs.get('Config') or []:
        kwargs = {}
        if cfg.get('Subnet'):
            kwargs['subnet'] = cfg['Subnet']
        if cfg.get('IPRange'):
            kwargs['iprange'] = cfg['IPRange']
        if cfg.get('Gateway'):
            kwargs['gateway'] = cfg['Gateway']
        if cfg.get('AuxiliaryAddresses'):
            kwargs['aux_addresses'] = cfg['AuxiliaryAddresses']
        if kwargs:
            pools.append(docker.types.IPAMPool(**kwargs))
    ipam = None
    if pools or ipam_attrs.get('Driver') or ipam_attrs.get('Options'):
        ipam = docker.types.IPAMConfig(
            driver=ipam_attrs.get('Driver') or 'default',
            pool_configs=pools or None,
            options=ipam_attrs.get('Options') or None,
        )
    return {
        'driver': attrs.get('Driver') or 'bridge',
        'options': attrs.get('Options') or None,
        'ipam': ipam,
        'internal': bool(attrs.get('Internal')),
        'enable_ipv6': bool(attrs.get('EnableIPv6')),
        'attachable': bool(attrs.get('Attachable')),
        'labels': attrs.get('Labels') or None,
    }


def ensure_networks(client, snapshot: Path, selected_names, log, run_id):
    inv = snapshot / 'inventaire' / 'networks.json'
    if not inv.is_file():
        return
    network_attrs = load_json(inv)
    needed = set()
    for name in selected_names:
        cinfo = snapshot / 'containers' / safe_name(name) / 'container-inspect.json'
        if cinfo.is_file():
            attrs = load_json(cinfo)
            needed.update((attrs.get('NetworkSettings', {}).get('Networks') or {}).keys())
    builtin = {'bridge', 'host', 'none'}
    for attrs in network_attrs:
        name = attrs.get('Name')
        if not name or name not in needed or name in builtin:
            continue
        try:
            client.networks.get(name)
            continue
        except docker.errors.NotFound:
            pass
        update_progress(72, 'Réseaux Docker', f'Création du réseau : {name}', run_id=run_id, log=log)
        kwargs = network_create_kwargs(attrs)
        try:
            client.networks.create(name, **kwargs)
        except Exception as exc:
            raise RuntimeError(f'impossible de recréer le réseau {name} : {exc}')


def compose_source_on_host(container_entry):
    compose = (container_entry.get('compose') or {}).get('compose_host_path') or ''
    return compose


def try_build_local_image(client, entry, snapshot: Path, inspect_attrs, log):
    image_ref = inspect_attrs.get('Config', {}).get('Image') or ''
    compose_host_path = compose_source_on_host(entry)
    service_name = (entry.get('compose') or {}).get('service') or ''
    if not image_ref or not compose_host_path or not service_name:
        return False

    compose_path = translate_host_path(compose_host_path)
    if not compose_path.is_file():
        return False
    try:
        raw = yaml.safe_load(compose_path.read_text()) or {}
        service = (raw.get('services') or {}).get(service_name) or {}
        build = service.get('build')
        if not build:
            return False
        if isinstance(build, str):
            context_value = build
            dockerfile = 'Dockerfile'
            buildargs = None
            target = None
        elif isinstance(build, dict):
            context_value = build.get('context', '.')
            dockerfile = build.get('dockerfile', 'Dockerfile')
            buildargs = build.get('args')
            target = build.get('target')
        else:
            return False
        if '${' in str(context_value):
            log.write(f'Build local ignoré pour {entry["name"]} : contexte Compose avec variable non résolue.\n')
            return False
        context = (compose_path.parent / context_value).resolve()
        if not context.is_dir():
            log.write(f'Build local ignoré pour {entry["name"]} : contexte absent {context}\n')
            return False
        log.write(f'Build local de {image_ref} depuis {context}\n')
        client.images.build(
            path=str(context),
            tag=image_ref,
            dockerfile=dockerfile,
            buildargs=buildargs,
            target=target,
            rm=True,
        )
        return True
    except Exception as exc:
        log.write(f'Build local échoué pour {entry["name"]} : {exc}\n')
        return False


def ensure_image(client, entry, snapshot: Path, inspect_attrs, log):
    image_ref = inspect_attrs.get('Config', {}).get('Image') or ''
    if not image_ref:
        raise RuntimeError(f'image absente dans inventaire pour {entry["name"]}')
    try:
        client.images.get(image_ref)
        return
    except docker.errors.ImageNotFound:
        pass
    try:
        log.write(f'Téléchargement image : {image_ref}\n')
        client.images.pull(image_ref)
        return
    except Exception as pull_exc:
        log.write(f'Pull impossible pour {image_ref} : {pull_exc}\n')
    if try_build_local_image(client, entry, snapshot, inspect_attrs, log):
        return
    raise RuntimeError(
        f'image {image_ref} indisponible et reconstruction locale impossible pour {entry["name"]}; '
        'voir son dossier compose/ et INFO.txt'
    )


def clean_host_config(host_config):
    hc = dict(host_config or {})
    # Ces champs peuvent contenir des références à l'ancien conteneur ou sont calculés par le daemon.
    for key in ('ContainerIDFile',):
        if hc.get(key) == '':
            hc.pop(key, None)
    # AutoRemove est dangereux lors d'une restauration : le conteneur pourrait disparaître au premier arrêt.
    hc['AutoRemove'] = False
    return hc


def clean_config(config):
    cfg = dict(config or {})
    # Les valeurs nulles sont acceptées par l'API, mais OnBuild est propre aux images.
    cfg.pop('OnBuild', None)
    return cfg


def networking_config_from_inspect(attrs):
    endpoints = {}
    for name, ep in (attrs.get('NetworkSettings', {}).get('Networks') or {}).items():
        if name in {'bridge', 'host', 'none'}:
            continue
        endpoint = {}
        if ep.get('IPAMConfig'):
            endpoint['IPAMConfig'] = ep['IPAMConfig']
        if ep.get('Links'):
            endpoint['Links'] = ep['Links']
        if ep.get('Aliases'):
            endpoint['Aliases'] = ep['Aliases']
        if ep.get('MacAddress'):
            endpoint['MacAddress'] = ep['MacAddress']
        if ep.get('DriverOpts'):
            endpoint['DriverOpts'] = ep['DriverOpts']
        endpoints[name] = endpoint
    return {'EndpointsConfig': endpoints} if endpoints else None


def create_from_inspect(client, name: str, attrs, log):
    cfg = clean_config(attrs.get('Config') or {})
    cfg['HostConfig'] = clean_host_config(attrs.get('HostConfig') or {})
    networking = networking_config_from_inspect(attrs)
    if networking:
        cfg['NetworkingConfig'] = networking
    # L'API bas niveau permet de réutiliser la configuration Docker sauvegardée sans interpréter le Compose.
    response = client.api.create_container_from_config(cfg, name=name)
    cid = response.get('Id') if isinstance(response, dict) else response
    if not cid:
        raise RuntimeError(f'Docker n’a pas retourné d’identifiant pour {name}')
    log.write(f'Conteneur recréé : {name} ({str(cid)[:12]})\n')
    return client.containers.get(cid)


def remove_existing(client, name, log):
    try:
        c = client.containers.get(name)
    except docker.errors.NotFound:
        return
    c.reload()
    if c.status == 'running':
        c.stop(timeout=30)
    log.write(f'Suppression du conteneur existant avant reconstruction : {name}\n')
    c.remove(force=True)


def is_aio_child(name):
    return name.startswith('nextcloud-aio-') and name != 'nextcloud-aio-mastercontainer'


def rebuild_containers(client, snapshot: Path, entries, log, run_id):
    selected_names = [e['name'] for e in entries]
    ensure_networks(client, snapshot, selected_names, log, run_id)

    master_selected = 'nextcloud-aio-mastercontainer' in selected_names
    rebuild_entries = []
    for e in entries:
        if master_selected and is_aio_child(e['name']):
            log.write(f'Nextcloud AIO : {e["name"]} restauré côté données mais non recréé directement ; le mastercontainer le gérera.\n')
            continue
        rebuild_entries.append(e)

    total = max(1, len(rebuild_entries))
    created = []
    for i, entry in enumerate(rebuild_entries, 1):
        name = entry['name']
        cdir = snapshot / 'containers' / entry.get('folder', safe_name(name))
        inspect_path = cdir / 'container-inspect.json'
        if not inspect_path.is_file():
            raise RuntimeError(f'inventaire Docker absent pour {name}')
        attrs = load_json(inspect_path)
        pct = 74 + int((i - 1) / total * 18)
        update_progress(pct, 'Reconstruction Docker', f'[{i}/{total}] {name} : image', container=name, operation='rebuild', run_id=run_id, log=log)
        ensure_image(client, entry, snapshot, attrs, log)
        remove_existing(client, name, log)
        update_progress(pct + 2, 'Reconstruction Docker', f'[{i}/{total}] {name} : création', container=name, operation='rebuild', run_id=run_id, log=log)
        c = create_from_inspect(client, name, attrs, log)
        created.append((c, attrs))

    update_progress(94, 'Démarrage', 'Démarrage des conteneurs reconstruits…', operation='rebuild', run_id=run_id, log=log)
    for c, attrs in created:
        old_state = attrs.get('State') or {}
        # On redémarre ce qui était actif lors de la sauvegarde. Les autres restent arrêtés.
        if old_state.get('Running') or old_state.get('Status') == 'running':
            try:
                c.start()
                log.write(f'Démarré : {c.name}\n')
            except Exception as exc:
                raise RuntimeError(f'impossible de démarrer {c.name} : {exc}')

    if master_selected:
        log.write('Nextcloud AIO : seul nextcloud-aio-mastercontainer a été recréé directement. Les conteneurs enfants seront gérés par AIO.\n')


def external_path_available(source: str):
    mapped = translate_host_path(source)
    if not mapped.exists():
        return False
    for host_prefix, container_prefix in (('/media/', '/host-media'), ('/mnt/', '/host-mnt')):
        if source.startswith(host_prefix):
            first = source[len(host_prefix):].split('/', 1)[0]
            top = Path(container_prefix) / first
            parent = Path(container_prefix)
            if not top.exists():
                return False
            try:
                return os.path.ismount(top) or top.stat().st_dev != parent.stat().st_dev
            except OSError:
                return False
    return True


def preflight_external_mounts(entries, log):
    missing = []
    for entry in entries:
        for m in entry.get('external_bind_mounts_not_copied', []) or []:
            source = m.get('source')
            if not source:
                continue
            # Vérification pour les espaces hôte explicitement montés dans le manager.
            if source.startswith(('/media/', '/mnt/', '/DATA/')) and not external_path_available(source):
                missing.append(source)
    if missing:
        unique = sorted(set(missing))
        log.write('Montages externes absents ou non montés :\n' + '\n'.join(f'- {p}' for p in unique) + '\n')
        raise RuntimeError('montage(s) externe(s) absent(s) ou non monté(s) : ' + ', '.join(unique[:5]) + ('…' if len(unique) > 5 else ''))


def main(snapshot_name, selected_names, action):
    global _messages
    _messages = []
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = open(OPERATION_LOCK, 'w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        print('Une opération est déjà en cours.')
        return 2

    started = datetime.now()
    log_path = LOG_DIR / f'restore-{started.strftime("%Y%m%d-%H%M%S")}.log'
    client = None
    run_id = None
    stopped_existing = []
    success = False
    error = ''

    try:
        s = settings()
        base = destination_path(s.get('destination', DEFAULT_DESTINATION))
        snapshot = snapshot_for_name(base, snapshot_name)
        manifest = load_json(snapshot / 'manifest.json')
        entries = selected_manifest_entries(manifest, selected_names)
        if not entries:
            raise RuntimeError('aucun conteneur sélectionné pour la restauration')

        with db() as conn:
            cur = conn.execute(
                'INSERT INTO restores(started_at,status,snapshot,action,containers,log_file,message) VALUES(?,?,?,?,?,?,?)',
                (started.isoformat(timespec='seconds'), 'running', snapshot_name, action, '\n'.join(selected_names), str(log_path), 'Restauration en cours')
            )
            run_id = cur.lastrowid

        with log_path.open('w', buffering=1) as log:
            log.write(f'ZimaOS Docker Backup Manager V{APP_VERSION}\n')
            log.write(f'Sauvegarde : {snapshot_name}\nAction : {action}\n')
            log.write('Conteneurs : ' + ', '.join(selected_names) + '\n\n')

            update_progress(2, 'Préparation', 'Lecture de la sauvegarde…', operation='restore', run_id=run_id, log=log)
            preflight_external_mounts(entries, log)
            client = docker.from_env()
            client.ping()

            stopped_existing = stop_existing(client, selected_names, log, run_id)
            update_progress(12, 'Restauration', 'Restauration des configurations et volumes…', operation='restore', run_id=run_id, log=log)
            restore_files(client, snapshot, entries, log, run_id)

            if action == 'full':
                update_progress(70, 'Reconstruction', 'Préparation de la reconstruction Docker…', operation='rebuild', run_id=run_id, log=log)
                rebuild_containers(client, snapshot, entries, log, run_id)
                # Les anciens conteneurs ont été remplacés ; ne pas essayer de les redémarrer par ID/nom ici.
                stopped_existing = []
            else:
                update_progress(92, 'Redémarrage', 'Redémarrage des conteneurs qui étaient actifs…', operation='restore', run_id=run_id, log=log)
                for name in stopped_existing:
                    try:
                        c = client.containers.get(name)
                        c.start()
                    except Exception as exc:
                        log.write(f'ATTENTION redémarrage {name} : {exc}\n')
                stopped_existing = []

            success = True
            finished = datetime.now()
            with db() as conn:
                conn.execute(
                    'UPDATE restores SET finished_at=?,status=?,message=? WHERE id=?',
                    (finished.isoformat(timespec='seconds'), 'success', f'{len(entries)} conteneur(s) traité(s)', run_id)
                )

    except Exception as exc:
        error = str(exc)
        if run_id is not None:
            try:
                with db() as conn:
                    conn.execute(
                        'UPDATE restores SET finished_at=?,status=?,message=? WHERE id=?',
                        (datetime.now().isoformat(timespec='seconds'), 'error', error, run_id)
                    )
            except Exception:
                pass
        try:
            with log_path.open('a') as log:
                log.write(f'\nERREUR : {error}\n')
        except Exception:
            pass

    finally:
        if client is not None and stopped_existing:
            try:
                with log_path.open('a') as log:
                    log.write('\nRécupération après erreur : redémarrage des conteneurs qui étaient actifs.\n')
                    for name in stopped_existing:
                        try:
                            c = client.containers.get(name)
                            c.start()
                        except Exception as exc:
                            log.write(f'ATTENTION redémarrage {name} : {exc}\n')
            except Exception:
                pass
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        try:
            fcntl.flock(lock_file, fcntl.LOCK_UN)
        finally:
            lock_file.close()
            try:
                OPERATION_LOCK.unlink()
            except FileNotFoundError:
                pass

    if success:
        update_progress(100, 'Terminé', 'Restauration terminée avec succès.', status='success', operation='restore', run_id=run_id)
        return 0
    update_progress(100, 'Erreur', f'ÉCHEC : {error or "erreur inconnue"}', status='error', operation='restore', run_id=run_id)
    return 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--snapshot', required=True)
    parser.add_argument('--container', action='append', dest='containers', default=[])
    parser.add_argument('--action', choices=('files', 'full'), default='files')
    args = parser.parse_args()
    sys.exit(main(args.snapshot, args.containers, args.action))
