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

APP_VERSION = '0.1.7'
DATA_DIR = Path('/data')
DB_PATH = DATA_DIR / 'manager.db'
LOCK_PATH = DATA_DIR / 'operation.lock'
LOG_DIR = DATA_DIR / 'logs'
PROGRESS_PATH = DATA_DIR / 'progress.json'
TIMESTAMP_RE = re.compile(r'^\d{8}-\d{6}$')
SELF_NAME = 'zimaos-docker-backup-manager'
DEFAULT_DESTINATION = '/media/zimaos-backup-docker'
COMPOSE_NAMES = ('docker-compose.yml', 'docker-compose.yaml', 'compose.yml', 'compose.yaml')

_progress_messages = []


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def settings():
    with db() as conn:
        return {r['key']: r['value'] for r in conn.execute('SELECT key,value FROM settings')}


def update_progress(percent, phase, message, status='running', container=None, backup_id=None, log=None):
    global _progress_messages
    if message:
        _progress_messages.append(message)
        _progress_messages = _progress_messages[-12:]
        if log is not None:
            log.write(message + '\n')
            log.flush()
    payload = {
        'status': status,
        'kind': 'backup',
        'percent': max(0, min(100, int(percent))),
        'phase': phase,
        'message': message,
        'container': container,
        'backup_id': backup_id,
        'updated_at': datetime.now().isoformat(timespec='seconds'),
        'messages': list(_progress_messages),
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = PROGRESS_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    os.replace(tmp, PROGRESS_PATH)


def translate_destination(host_path: str) -> Path:
    p = host_path.rstrip('/')
    mappings = [
        ('/media', '/host-media'),
        ('/mnt', '/host-mnt'),
        ('/DATA', '/host-data'),
    ]
    for host_prefix, container_prefix in mappings:
        if p == host_prefix or p.startswith(host_prefix + '/'):
            suffix = p[len(host_prefix):]
            return Path(container_prefix + suffix)
    raise RuntimeError('la destination doit commencer par /media/, /mnt/ ou /DATA/')


def _top_level_mount(host_path: str):
    if host_path.startswith('/media/'):
        rel = host_path[len('/media/'):].split('/', 1)[0]
        return Path('/host-media') / rel, Path('/host-media')
    if host_path.startswith('/mnt/'):
        rel = host_path[len('/mnt/'):].split('/', 1)[0]
        return Path('/host-mnt') / rel, Path('/host-mnt')
    return None, None


def validate_destination(host_path: str, mapped: Path, create=True):
    if host_path == '/DATA/AppData' or host_path.startswith('/DATA/AppData/'):
        raise RuntimeError('destination interdite : /DATA/AppData ne peut pas se sauvegarder dans lui-même')
    if host_path.startswith(('/media/', '/mnt/')):
        top, parent = _top_level_mount(host_path)
        if top is None or not top.exists():
            raise RuntimeError(f'le point de montage {top or host_path} n’existe pas')
        try:
            mounted = os.path.ismount(top) or top.stat().st_dev != parent.stat().st_dev
        except OSError:
            mounted = False
        if not mounted:
            raise RuntimeError(
                f'{top} existe mais ne semble pas être un disque/NAS monté ; '
                'sauvegarde annulée pour éviter d’écrire sur le disque système'
            )
    if create:
        mapped.mkdir(parents=True, exist_ok=True)
    elif not mapped.exists():
        raise RuntimeError('le dossier de destination n’existe pas')
    if not os.access(mapped, os.W_OK):
        raise RuntimeError('destination non inscriptible')
    probe = mapped / f'.zdbm-test-{os.getpid()}'
    try:
        probe.write_text('ok')
        probe.unlink()
    except Exception as exc:
        raise RuntimeError(f'test d’écriture impossible : {exc}')


def run_rsync(src: Path, dst: Path, log, excludes=None):
    dst.mkdir(parents=True, exist_ok=True)
    cmd = ['rsync', '-aH', '--numeric-ids', '--stats']
    for pattern in excludes or []:
        if pattern.strip():
            cmd.extend(['--exclude', pattern.strip()])
    cmd += [str(src).rstrip('/') + '/', str(dst).rstrip('/') + '/']
    log.write('$ ' + ' '.join(cmd) + '\n')
    log.flush()
    proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f'rsync a échoué ({proc.returncode}) : {src}')


def run_rsync_relative(root: Path, relative_paths, dst: Path, log, excludes=None):
    relative_paths = list(relative_paths)
    if not relative_paths:
        return
    if any(str(p) in ('.', '') for p in relative_paths):
        run_rsync(root, dst, log, excludes=excludes)
        return

    dst.mkdir(parents=True, exist_ok=True)
    cmd = ['rsync', '-aH', '--numeric-ids', '--stats', '--relative']
    for pattern in excludes or []:
        if pattern.strip():
            cmd.extend(['--exclude', pattern.strip()])
    for rel in relative_paths:
        cmd.append(str(root) + '/./' + str(rel).lstrip('/'))
    cmd.append(str(dst).rstrip('/') + '/')
    log.write('$ ' + ' '.join(cmd) + '\n')
    log.flush()
    proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT)
    if proc.returncode != 0:
        raise RuntimeError(f'rsync sélectif a échoué ({proc.returncode})')


def dir_size(path: Path):
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += (Path(root) / f).stat().st_size
            except OSError:
                pass
    return total


def save_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def safe_name(name: str):
    return re.sub(r'[^A-Za-z0-9_.-]+', '_', name).strip('._') or 'container'


def selected_containers(client, s, log):
    all_containers = [c for c in client.containers.list(all=True) if c.name != SELF_NAME]
    raw = s.get('selected_containers', '__ALL__')
    if raw == '__ALL__':
        selected = all_containers
    else:
        wanted = {line.strip() for line in raw.splitlines() if line.strip()}
        selected = [c for c in all_containers if c.name in wanted]
        found = {c.name for c in selected}
        missing = sorted(wanted - found)
        for name in missing:
            log.write(f'ATTENTION : conteneur sélectionné introuvable : {name}\n')
    return sorted(selected, key=lambda c: c.name.lower())


def inventory(client, containers, inv: Path):
    inv.mkdir(parents=True, exist_ok=True)
    save_json(inv / 'containers-inspect.json', [c.attrs for c in containers])
    save_json(inv / 'containers-summary.json', [{
        'name': c.name,
        'id': c.id,
        'image': c.attrs.get('Config', {}).get('Image'),
        'status': c.status,
        'ports': c.attrs.get('NetworkSettings', {}).get('Ports'),
        'mounts': c.attrs.get('Mounts', []),
        'labels': c.attrs.get('Config', {}).get('Labels') or {},
    } for c in containers])

    network_names = set()
    volume_names = set()
    image_ids = set()
    for c in containers:
        network_names.update((c.attrs.get('NetworkSettings', {}).get('Networks') or {}).keys())
        for m in c.attrs.get('Mounts') or []:
            if m.get('Type') == 'volume' and m.get('Name'):
                volume_names.add(m['Name'])
        try:
            image_ids.add(c.image.id)
        except Exception:
            pass

    networks = []
    for name in sorted(network_names):
        try:
            networks.append(client.networks.get(name).attrs)
        except Exception:
            networks.append({'Name': name, 'warning': 'non lisible pendant inventaire'})
    save_json(inv / 'networks.json', networks)

    volumes = []
    for name in sorted(volume_names):
        try:
            volumes.append(client.volumes.get(name).attrs)
        except Exception:
            volumes.append({'Name': name, 'warning': 'non lisible pendant inventaire'})
    save_json(inv / 'volumes.json', volumes)

    images = []
    for image_id in sorted(image_ids):
        try:
            i = client.images.get(image_id)
            images.append({'id': i.id, 'tags': i.tags, 'repo_digests': i.attrs.get('RepoDigests', [])})
        except Exception:
            pass
    save_json(inv / 'images.json', images)

    lines = ['NAME\tSTATUS\tIMAGE']
    for c in containers:
        lines.append(f'{c.name}\t{c.status}\t{c.attrs.get("Config", {}).get("Image", "-")}')
    (inv / 'docker-ps.txt').write_text('\n'.join(lines) + '\n')


def normalize_relative_paths(paths):
    cleaned = []
    for p in paths:
        p = PurePosixPath(str(p))
        if str(p) in ('', '.'):
            return [PurePosixPath('.')]
        if p.is_absolute() or '..' in p.parts:
            continue
        cleaned.append(p)

    unique = sorted(set(cleaned), key=lambda p: (len(p.parts), str(p)))
    kept = []
    for p in unique:
        if any(parent == p or parent in p.parents for parent in kept):
            continue
        kept.append(p)
    return kept


def container_appdata_paths(container):
    rels = []
    external = []
    for m in container.attrs.get('Mounts') or []:
        if m.get('Type') != 'bind' or not m.get('Source'):
            continue
        source = m['Source']
        if source == '/DATA/AppData':
            rels.append(PurePosixPath('.'))
        elif source.startswith('/DATA/AppData/'):
            rels.append(PurePosixPath(source[len('/DATA/AppData/'):]))
        else:
            external.append({
                'source': source,
                'destination': m.get('Destination'),
                'mode': m.get('Mode'),
            })
    return normalize_relative_paths(rels), external


def container_volume_names(container):
    return sorted({
        m.get('Name')
        for m in (container.attrs.get('Mounts') or [])
        if m.get('Type') == 'volume' and m.get('Name')
    })


def raw_compose_paths_for_container(container):
    labels = container.attrs.get('Config', {}).get('Labels') or {}
    raw_files = labels.get('com.docker.compose.project.config_files') or ''
    return [value.strip() for value in raw_files.split(',') if value.strip()]


def find_compose_for_container(container):
    labels = container.attrs.get('Config', {}).get('Labels') or {}
    project = labels.get('com.docker.compose.project') or ''
    service = labels.get('com.docker.compose.service') or ''

    for host_path in raw_compose_paths_for_container(container):
        if host_path.startswith('/var/lib/casaos/apps/'):
            rel = PurePosixPath(host_path[len('/var/lib/casaos/apps/'):])
            if (Path('/host-compose') / str(rel)).is_file():
                return host_path, project, service, 'zimaos', rel
        elif host_path.startswith('/DATA/AppData/'):
            rel = PurePosixPath(host_path[len('/DATA/AppData/'):])
            if (Path('/host-appdata') / str(rel)).is_file():
                return host_path, project, service, 'appdata', rel

    for folder in (project, container.name):
        if not folder:
            continue
        base = Path('/host-compose') / folder
        if not base.is_dir():
            continue
        for filename in COMPOSE_NAMES:
            rel = PurePosixPath(folder) / filename
            if (Path('/host-compose') / str(rel)).is_file():
                return '/var/lib/casaos/apps/' + str(rel), project, service, 'zimaos', rel

    return '', project, service, '', None


def copy_compose_for_container(container, container_dir: Path, log):
    host_path, project, service, source_type, rel = find_compose_for_container(container)
    extra_appdata = []
    if not host_path:
        log.write(f'Compose non trouvé pour {container.name}; inventaire Docker conservé comme secours.\n')
        return {
            'container': container.name,
            'project': project,
            'service': service,
            'compose_host_path': '',
        }, extra_appdata

    if source_type == 'zimaos':
        source_dir = Path('/host-compose') / str(rel.parent)
        target_dir = container_dir / 'compose' / 'zimaos' / str(rel.parent)
        run_rsync(source_dir, target_dir, log)
    elif source_type == 'appdata':
        # Le projet complet sous AppData sera copié avec les données du conteneur.
        # On garde aussi une copie du Compose dans un dossier lisible.
        source_file = Path('/host-appdata') / str(rel)
        target_file = container_dir / 'compose' / 'appdata' / str(rel)
        target_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, target_file)
        extra_appdata.append(rel.parent)

    return {
        'container': container.name,
        'project': project,
        'service': service,
        'compose_host_path': host_path,
    }, extra_appdata


def copy_named_volume(client, name, target: Path, log):
    try:
        client.volumes.get(name)
    except Exception:
        log.write(f'Volume ignoré (introuvable dans Docker) : {name}\n')
        return False
    src = Path('/host-docker-root/volumes') / name / '_data'
    if not src.is_dir():
        log.write(f'Volume ignoré (pas de _data local lisible) : {name}\n')
        return False
    run_rsync(src, target / name / '_data', log)
    return True


def write_container_info(container, container_dir: Path, appdata_rels, external_binds, volumes, compose_row):
    container_dir.mkdir(parents=True, exist_ok=True)
    save_json(container_dir / 'container-inspect.json', container.attrs)
    save_json(container_dir / 'container.json', {
        'name': container.name,
        'image': container.attrs.get('Config', {}).get('Image'),
        'status_before_backup': container.status,
        'compose': compose_row,
        'appdata_paths': [str(p) for p in appdata_rels],
        'named_volumes': volumes,
        'external_bind_mounts_not_copied': external_binds,
    })

    lines = [
        f'CONTENEUR : {container.name}',
        '=' * (12 + len(container.name)),
        '',
        f'Image : {container.attrs.get("Config", {}).get("Image", "-")}',
        f'Compose original : {compose_row.get("compose_host_path") or "non trouvé"}',
        '',
        'CONFIGURATION /DATA/AppData :',
    ]
    if appdata_rels:
        for rel in appdata_rels:
            shown = '/DATA/AppData' if str(rel) == '.' else f'/DATA/AppData/{rel}'
            lines.append(f'- {shown}')
    else:
        lines.append('- aucune')
    lines += ['', 'VOLUMES DOCKER NOMMÉS :']
    lines += [f'- {v}' for v in volumes] if volumes else ['- aucun']
    lines += ['', 'MONTAGES EXTERNES NON COPIÉS :']
    if external_binds:
        for m in external_binds:
            lines.append(f'- {m.get("source")} -> {m.get("destination")}')
    else:
        lines.append('- aucun')
    (container_dir / 'INFO.txt').write_text('\n'.join(lines) + '\n')
    (container_dir / 'volumes.txt').write_text('\n'.join(volumes) + ('\n' if volumes else ''))


def copy_restore_scripts(snapshot: Path):
    shutil.copy2('/app/restore-template.sh', snapshot / 'restore-zimaos.sh')
    shutil.copy2('/app/rebuild-template.sh', snapshot / 'rebuild-stacks.sh')
    os.chmod(snapshot / 'restore-zimaos.sh', 0o755)
    os.chmod(snapshot / 'rebuild-stacks.sh', 0o755)
    (snapshot / 'LISEZ-MOI.txt').write_text(
        'KIT DE RECONSTRUCTION ZIMAOS\n'
        '============================\n\n'
        'Organisation V0.1.6 : chaque conteneur possède son propre dossier lisible.\n\n'
        'containers/<nom-du-conteneur>/\n'
        '  INFO.txt             résumé humain\n'
        '  container.json       sources et volumes\n'
        '  container-inspect.json configuration Docker complète\n'
        '  appdata/             configuration /DATA/AppData de ce conteneur\n'
        '  compose/             Compose associé à ce conteneur\n'
        '  volumes.txt          volumes Docker utilisés\n\n'
        'shared-volumes/ contient les données des volumes Docker nommés.\n'
        'inventaire/ contient l’inventaire global de la sélection.\n\n'
        'Après une panne totale :\n'
        '1. Réinstaller ZimaOS.\n'
        '2. Remonter les disques/NAS aux mêmes chemins (/media, etc.).\n'
        '3. Depuis ce dossier : sudo ./restore-zimaos.sh\n'
        '4. Puis : sudo ./rebuild-stacks.sh\n\n'
        'Les bind mounts hors /DATA/AppData ne sont pas copiés ; ils sont documentés dans chaque dossier conteneur.\n'
        'ATTENTION : cette sauvegarde peut contenir mots de passe, tokens et secrets.\n'
    )


def cleanup_retention(base: Path, keep: int, current: Path, log):
    snapshots = sorted(
        [p for p in base.iterdir() if p.is_dir() and TIMESTAMP_RE.match(p.name)],
        reverse=True,
    )
    for old in snapshots[keep:]:
        if old == current:
            continue
        log.write(f'Suppression rétention : {old}\n')
        log.flush()
        shutil.rmtree(old, ignore_errors=False)


def main(mode):
    global _progress_messages
    _progress_messages = []
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    lock_file = open(LOCK_PATH, 'w')
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        print('Une sauvegarde est déjà en cours.')
        return 2

    started = datetime.now()
    stamp = started.strftime('%Y%m%d-%H%M%S')
    log_path = LOG_DIR / f'backup-{stamp}.log'
    running_ids = []
    client = None
    backup_id = None
    s = {}
    snapshot = None
    return_code = 1
    last_error = ''

    try:
        s = settings()
        host_dest = s.get('destination', DEFAULT_DESTINATION)
        mapped_dest = translate_destination(host_dest)
        snapshot = mapped_dest / stamp

        with db() as conn:
            cur = conn.execute(
                'INSERT INTO backups(started_at,status,destination,snapshot,log_file,message) VALUES(?,?,?,?,?,?)',
                (started.isoformat(timespec='seconds'), 'running', host_dest, stamp, str(log_path), f'Sauvegarde {mode}')
            )
            backup_id = cur.lastrowid

        with log_path.open('w', buffering=1) as log:
            log.write(f'ZimaOS Docker Backup Manager V{APP_VERSION} - {started.isoformat()}\n')
            log.write(f'Destination hôte : {host_dest}\n')
            log.write(f'Destination conteneur : {mapped_dest}\n\n')

            update_progress(2, 'Préparation', 'Vérification de la destination…', backup_id=backup_id, log=log)
            validate_destination(host_dest, mapped_dest, create=True)
            snapshot.mkdir(parents=True, exist_ok=False)

            client = docker.from_env()
            client.ping()
            containers = selected_containers(client, s, log)
            if not containers:
                raise RuntimeError('aucun conteneur sélectionné')

            update_progress(7, 'Inventaire', f'{len(containers)} conteneur(s) sélectionné(s).', backup_id=backup_id, log=log)
            inventory(client, containers, snapshot / 'inventaire')

            running = [c for c in containers if c.status == 'running']
            running_ids = [c.id for c in running]

            if s.get('stop_containers', '1') == '1':
                update_progress(12, 'Arrêt', f'Arrêt propre de {len(running)} conteneur(s)…', backup_id=backup_id, log=log)
                for c in running:
                    update_progress(12, 'Arrêt', f'Arrêt : {c.name}', container=c.name, backup_id=backup_id, log=log)
                    c.stop(timeout=30)
            else:
                update_progress(12, 'Sauvegarde à chaud', 'Les conteneurs restent actifs.', backup_id=backup_id, log=log)

            excludes = [x for x in s.get('appdata_excludes', '').splitlines() if x.strip()]
            rebuild_plan = []
            manifest_containers = []
            all_volume_names = []
            external_binds_global = []

            total = len(containers)
            for index, c in enumerate(containers, start=1):
                start_pct = 16 + int(((index - 1) / total) * 60)
                end_pct = 16 + int((index / total) * 60)
                cdir = snapshot / 'containers' / safe_name(c.name)
                cdir.mkdir(parents=True, exist_ok=True)

                update_progress(start_pct, 'Conteneurs', f'[{index}/{total}] {c.name} : lecture du Compose', container=c.name, backup_id=backup_id, log=log)
                compose_row, compose_extra_rels = copy_compose_for_container(c, cdir, log)
                rebuild_plan.append(compose_row)

                appdata_rels, external_binds = container_appdata_paths(c)
                appdata_rels = normalize_relative_paths(list(appdata_rels) + list(compose_extra_rels))
                volumes = container_volume_names(c)
                all_volume_names.extend(volumes)
                external_binds_global.extend([{'container': c.name, **m} for m in external_binds])

                update_progress(
                    min(end_pct - 2, 74),
                    'Conteneurs',
                    f'[{index}/{total}] {c.name} : sauvegarde de la configuration AppData',
                    container=c.name,
                    backup_id=backup_id,
                    log=log,
                )
                if appdata_rels:
                    run_rsync_relative(Path('/host-appdata'), appdata_rels, cdir / 'appdata', log, excludes=excludes)
                else:
                    (cdir / 'appdata').mkdir(parents=True, exist_ok=True)

                write_container_info(c, cdir, appdata_rels, external_binds, volumes, compose_row)
                manifest_containers.append({
                    'name': c.name,
                    'folder': safe_name(c.name),
                    'appdata_paths': [str(p) for p in appdata_rels],
                    'named_volumes': volumes,
                    'external_bind_mounts_not_copied': external_binds,
                    'compose': compose_row,
                })
                update_progress(end_pct, 'Conteneurs', f'[{index}/{total}] {c.name} : terminé', container=c.name, backup_id=backup_id, log=log)

            volume_names = sorted(set(all_volume_names))
            volume_target = snapshot / 'shared-volumes'
            if volume_names:
                for index, name in enumerate(volume_names, start=1):
                    pct = 78 + int((index / len(volume_names)) * 12)
                    update_progress(pct, 'Volumes Docker', f'Volume [{index}/{len(volume_names)}] : {name}', backup_id=backup_id, log=log)
                    copy_named_volume(client, name, volume_target, log)
            else:
                volume_target.mkdir(parents=True, exist_ok=True)
                update_progress(90, 'Volumes Docker', 'Aucun volume Docker nommé à copier.', backup_id=backup_id, log=log)

            update_progress(92, 'Kit de reconstruction', 'Création des scripts et du plan de reconstruction…', backup_id=backup_id, log=log)
            with (snapshot / 'rebuild-plan.tsv').open('w') as f:
                f.write('compose_host_path\tservice\tcontainer\n')
                for row in rebuild_plan:
                    f.write(f"{row['compose_host_path']}\t{row['service']}\t{row['container']}\n")
            save_json(snapshot / 'rebuild-plan.json', rebuild_plan)
            copy_restore_scripts(snapshot)

            manifest = {
                'version': APP_VERSION,
                'created_at': started.isoformat(timespec='seconds'),
                'host_destination': host_dest,
                'containers_were_stopped': s.get('stop_containers', '1') == '1',
                'selected_containers': [c.name for c in containers],
                'running_containers_before_backup': [c.name for c in running],
                'containers': manifest_containers,
                'named_volumes': volume_names,
                'external_bind_mounts_not_copied': external_binds_global,
                'compose_rebuild_plan': rebuild_plan,
                'appdata_excludes': excludes,
            }
            save_json(snapshot / 'manifest.json', manifest)

            update_progress(95, 'Finalisation', 'Calcul de la taille et application de la rétention…', backup_id=backup_id, log=log)
            size = dir_size(snapshot)
            (mapped_dest / 'LATEST').write_text(stamp + '\n')
            keep = max(1, int(s.get('retention', '7')))
            cleanup_retention(mapped_dest, keep, snapshot, log)

            finished = datetime.now()
            log.write(f'\nSUCCÈS - Taille logique : {size} octets\n')
            with db() as conn:
                conn.execute(
                    'UPDATE backups SET finished_at=?,status=?,size_bytes=?,message=? WHERE id=?',
                    (
                        finished.isoformat(timespec='seconds'),
                        'success',
                        size,
                        f'{len(containers)} conteneur(s) sauvegardé(s)',
                        backup_id,
                    )
                )
            return_code = 0

    except Exception as exc:
        last_error = str(exc)
        try:
            if snapshot is not None and snapshot.exists():
                (snapshot / 'ECHEC.txt').write_text(str(exc) + '\n')
        except Exception:
            pass
        if backup_id is not None:
            try:
                with db() as conn:
                    conn.execute(
                        'UPDATE backups SET finished_at=?,status=?,message=? WHERE id=?',
                        (datetime.now().isoformat(timespec='seconds'), 'error', str(exc), backup_id)
                    )
            except Exception:
                pass
        try:
            with log_path.open('a') as log:
                log.write(f'\nERREUR : {exc}\n')
        except Exception:
            pass
        update_progress(100, 'Erreur', f'ÉCHEC : {exc}', status='error', backup_id=backup_id)
        return_code = 1

    finally:
        if client is not None and running_ids and s.get('stop_containers', '1') == '1':
            try:
                with log_path.open('a') as log:
                    update_progress(97, 'Redémarrage', 'Redémarrage des conteneurs qui étaient actifs…', backup_id=backup_id, log=log)
                    for cid in running_ids:
                        try:
                            c = client.containers.get(cid)
                            c.reload()
                            if c.status != 'running':
                                update_progress(98, 'Redémarrage', f'Redémarrage : {c.name}', container=c.name, backup_id=backup_id, log=log)
                                c.start()
                        except Exception as exc:
                            log.write(f'  ERREUR redémarrage {cid[:12]} : {exc}\n')
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
                LOCK_PATH.unlink()
            except FileNotFoundError:
                pass

    if return_code == 0:
        update_progress(100, 'Terminé', 'Sauvegarde terminée avec succès.', status='success', backup_id=backup_id)
    else:
        update_progress(100, 'Erreur', f'ÉCHEC : {last_error or "erreur inconnue"}', status='error', backup_id=backup_id)
    return return_code


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--manual', action='store_true')
    parser.add_argument('--scheduled', action='store_true')
    args = parser.parse_args()
    mode = 'scheduled' if args.scheduled else 'manual'
    sys.exit(main(mode))
