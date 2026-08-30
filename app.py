import argparse
import fcntl
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

import docker
from flask import Flask, flash, jsonify, redirect, render_template, request, url_for

DATA_DIR = Path('/data')
DB_PATH = DATA_DIR / 'manager.db'
CRON_FILE = Path('/etc/cron.d/zdbm-backup')
PROGRESS_FILE = DATA_DIR / 'progress.json'
OPERATION_LOCK = DATA_DIR / 'operation.lock'
TIMESTAMP_RE = re.compile(r'^\d{8}-\d{6}$')
SELF_NAME = 'zimaos-docker-backup-manager'
OLD_DEFAULT_DESTINATION = '/media/sauvegardes-nas/ZimaOS/docker-backup'
NEW_DEFAULT_DESTINATION = '/media/zimaos-backup-docker'

DEFAULTS = {
    'destination': NEW_DEFAULT_DESTINATION,
    'schedule_enabled': '0',
    'frequency': 'daily',
    'time': '03:00',
    'weekday': '0',
    'retention': '7',
    'stop_containers': '1',
    'appdata_excludes': 'zimaos-docker-backup-manager/data/logs/**\nzimaos-docker-backup-manager/data/tmp/**',
    # __ALL__ signifie : à la première ouverture, tous les conteneurs détectés sont cochés.
    # Dès que l'utilisateur enregistre, on stocke explicitement les noms cochés, un par ligne.
    'selected_containers': '__ALL__',
}

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'zdbm-local-admin')


def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')
    return conn


def init_db():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / 'logs').mkdir(exist_ok=True)
    (DATA_DIR / 'tmp').mkdir(exist_ok=True)
    with db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS backups (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                destination TEXT,
                snapshot TEXT,
                size_bytes INTEGER DEFAULT 0,
                message TEXT,
                log_file TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS restores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL,
                snapshot TEXT NOT NULL,
                action TEXT NOT NULL,
                containers TEXT,
                message TEXT,
                log_file TEXT
            )
        ''')
        for k, v in DEFAULTS.items():
            conn.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)', (k, v))

        # Migration V0.1.2 : si l'utilisateur avait encore exactement l'ancien chemin
        # par défaut, on le remplace par le nouveau. Un chemin personnalisé est conservé.
        row = conn.execute('SELECT value FROM settings WHERE key=?', ('destination',)).fetchone()
        if row and row['value'] == OLD_DEFAULT_DESTINATION:
            conn.execute('UPDATE settings SET value=? WHERE key=?', (NEW_DEFAULT_DESTINATION, 'destination'))
    write_cron()


def get_settings():
    with db() as conn:
        rows = conn.execute('SELECT key,value FROM settings').fetchall()
    values = DEFAULTS.copy()
    values.update({r['key']: r['value'] for r in rows})
    return values


def set_settings(values):
    with db() as conn:
        for k, v in values.items():
            conn.execute(
                'INSERT INTO settings(key,value) VALUES(?,?) '
                'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                (k, str(v)),
            )


def cron_expression(s):
    try:
        hour, minute = [int(x) for x in s['time'].split(':', 1)]
    except Exception:
        hour, minute = 3, 0
    freq = s.get('frequency', 'daily')
    if freq == 'weekly':
        return f'{minute} {hour} * * {int(s.get("weekday", "0"))}'
    if freq == 'monthly':
        return f'{minute} {hour} 1 * *'
    return f'{minute} {hour} * * *'


def write_cron():
    try:
        s = get_settings()
    except Exception:
        return
    if s.get('schedule_enabled') == '1':
        expr = cron_expression(s)
        content = (
            'SHELL=/bin/sh\n'
            'PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin\n'
            f'{expr} root cd /app && /usr/local/bin/python /app/backup.py --scheduled >> /data/cron.log 2>&1\n'
        )
        CRON_FILE.write_text(content)
        os.chmod(CRON_FILE, 0o644)
    else:
        try:
            CRON_FILE.unlink()
        except FileNotFoundError:
            pass


def backup_running():
    # Depuis la V0.1.6, sauvegarde et restauration partagent le même verrou.
    # On vérifie aussi l'ancien backup.lock pour une mise à jour pendant une sauvegarde V0.1.5.
    for lock_path in (OPERATION_LOCK, DATA_DIR / 'backup.lock'):
        if not lock_path.exists():
            continue
        try:
            with lock_path.open('a+') as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass
        except OSError:
            continue
    return False


def read_progress():
    try:
        if PROGRESS_FILE.exists():
            import json
            return json.loads(PROGRESS_FILE.read_text())
    except Exception:
        pass
    return {
        'status': 'idle',
        'kind': 'backup',
        'percent': 0,
        'phase': 'Prêt',
        'message': '',
        'container': None,
        'messages': [],
    }


def human_size(n):
    n = int(n or 0)
    units = ['o', 'Ko', 'Mo', 'Go', 'To']
    val = float(n)
    for unit in units:
        if val < 1024 or unit == units[-1]:
            return f'{val:.1f} {unit}' if unit != 'o' else f'{int(val)} {unit}'
        val /= 1024


@app.template_filter('size')
def _size(n):
    return human_size(n)


def selected_names_from_settings(s, available_names):
    raw = s.get('selected_containers', '__ALL__')
    if raw == '__ALL__':
        return set(available_names)
    return {line.strip() for line in raw.splitlines() if line.strip()}


def docker_container_rows(s):
    rows = []
    client = None
    error = None
    try:
        client = docker.from_env()
        client.ping()
        containers = sorted(client.containers.list(all=True), key=lambda c: c.name.lower())
        available = [c.name for c in containers if c.name != SELF_NAME]
        selected = selected_names_from_settings(s, available)

        for c in containers:
            if c.name == SELF_NAME:
                continue
            attrs = c.attrs
            labels = attrs.get('Config', {}).get('Labels') or {}
            mounts = attrs.get('Mounts') or []
            appdata = sorted({
                m.get('Source') for m in mounts
                if m.get('Type') == 'bind'
                and m.get('Source')
                and (m.get('Source') == '/DATA/AppData' or m.get('Source').startswith('/DATA/AppData/'))
            })
            volumes = sorted({
                m.get('Name') for m in mounts
                if m.get('Type') == 'volume' and m.get('Name')
            })
            external_binds = sorted({
                m.get('Source') for m in mounts
                if m.get('Type') == 'bind'
                and m.get('Source')
                and not (m.get('Source') == '/DATA/AppData' or m.get('Source').startswith('/DATA/AppData/'))
            })
            project = labels.get('com.docker.compose.project') or ''
            service = labels.get('com.docker.compose.service') or ''
            compose_files = labels.get('com.docker.compose.project.config_files') or ''
            rows.append({
                'name': c.name,
                'status': c.status,
                'image': attrs.get('Config', {}).get('Image') or '-',
                'project': project or '-',
                'service': service or '-',
                'compose': bool(compose_files or project),
                'appdata': appdata,
                'volumes': volumes,
                'external_binds': external_binds,
                'selected': c.name in selected,
            })
    except Exception as exc:
        error = str(exc)
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
    return rows, error


def parse_settings_form(form):
    freq = form.get('frequency', 'daily')
    if freq not in {'daily', 'weekly', 'monthly'}:
        freq = 'daily'

    destination = form.get('destination', '').strip()
    if destination.startswith('/DATA/AppData'):
        raise ValueError('Destination refusée : ne sauvegarde pas AppData à l’intérieur de lui-même.')
    if not destination.startswith(('/media/', '/mnt/', '/DATA/')):
        raise ValueError('Destination refusée : utilise un chemin commençant par /media/, /mnt/ ou /DATA/.')

    retention = form.get('retention', '7')
    try:
        retention_i = min(100, max(1, int(retention)))
    except ValueError:
        retention_i = 7

    selected = sorted(set(form.getlist('selected_containers')))

    return {
        'destination': destination.rstrip('/'),
        'schedule_enabled': '1' if form.get('schedule_enabled') == 'on' else '0',
        'frequency': freq,
        'time': form.get('time', '03:00'),
        'weekday': form.get('weekday', '0'),
        'retention': str(retention_i),
        'stop_containers': '1' if form.get('stop_containers') == 'on' else '0',
        'appdata_excludes': form.get('appdata_excludes', '').strip(),
        'selected_containers': '\n'.join(selected),
    }


def save_form_or_flash():
    try:
        values = parse_settings_form(request.form)
        set_settings(values)
        write_cron()
        return True
    except ValueError as exc:
        flash(str(exc), 'error')
        return False


def mapped_backup_base(s):
    from backup import translate_destination
    return translate_destination(s.get('destination', NEW_DEFAULT_DESTINATION))


def snapshot_list(s):
    rows = []
    error = None
    try:
        base = mapped_backup_base(s)
        if not base.is_dir():
            return [], None
        for p in sorted(base.iterdir(), reverse=True):
            if not p.is_dir() or not TIMESTAMP_RE.match(p.name):
                continue
            manifest_path = p / 'manifest.json'
            if not manifest_path.is_file():
                continue
            try:
                manifest = json.loads(manifest_path.read_text())
            except Exception:
                continue
            rows.append({
                'name': p.name,
                'created_at': manifest.get('created_at') or p.name,
                'version': manifest.get('version') or '?',
                'containers': manifest.get('containers') or [],
                'container_count': len(manifest.get('containers') or []),
            })
    except Exception as exc:
        error = str(exc)
    return rows, error


def snapshot_detail(s, snapshot_name):
    if not TIMESTAMP_RE.match(snapshot_name or ''):
        raise ValueError('Sauvegarde invalide.')
    base = mapped_backup_base(s)
    snap = base / snapshot_name
    manifest_path = snap / 'manifest.json'
    if not snap.is_dir() or not manifest_path.is_file():
        raise ValueError('Sauvegarde introuvable ou incomplète.')
    manifest = json.loads(manifest_path.read_text())
    rows = []
    for entry in manifest.get('containers') or []:
        name = entry.get('name')
        if not name:
            continue
        rows.append({
            'name': name,
            'image': '',
            'appdata_paths': entry.get('appdata_paths') or [],
            'named_volumes': entry.get('named_volumes') or [],
            'external_binds': entry.get('external_bind_mounts_not_copied') or [],
            'compose': entry.get('compose') or {},
            'aio_child': name.startswith('nextcloud-aio-') and name != 'nextcloud-aio-mastercontainer',
        })
        cjson = snap / 'containers' / entry.get('folder', name) / 'container.json'
        if cjson.is_file():
            try:
                cdata = json.loads(cjson.read_text())
                rows[-1]['image'] = cdata.get('image') or ''
            except Exception:
                pass
    return snap, manifest, rows


@app.route('/')
def index():
    s = get_settings()
    with db() as conn:
        backups = conn.execute('SELECT * FROM backups ORDER BY id DESC LIMIT 20').fetchall()
    containers, docker_error = docker_container_rows(s)
    selected_count = sum(1 for c in containers if c['selected'])
    running_count = sum(1 for c in containers if c['status'] == 'running')
    return render_template(
        'index.html',
        s=s,
        backups=backups,
        running=backup_running(),
        progress=read_progress(),
        cron=cron_expression(s),
        containers=containers,
        docker_error=docker_error,
        selected_count=selected_count,
        running_count=running_count,
    )


@app.post('/settings')
def settings_save():
    if not save_form_or_flash():
        return redirect(url_for('index'))
    flash('Configuration et sélection des conteneurs enregistrées.', 'success')
    return redirect(url_for('index'))


@app.post('/backup-now')
def backup_now():
    if backup_running():
        flash('Une sauvegarde est déjà en cours.', 'error')
        return redirect(url_for('index'))

    # Le bouton "Sauvegarder maintenant" transmet tout le formulaire : la sélection
    # visible à l'écran est donc enregistrée juste avant le lancement.
    if not save_form_or_flash():
        return redirect(url_for('index'))

    s = get_settings()
    if not s.get('selected_containers', '').strip():
        flash('Aucun conteneur sélectionné : sauvegarde non lancée.', 'error')
        return redirect(url_for('index'))

    subprocess.Popen(
        ['/usr/local/bin/python', '/app/backup.py', '--manual'],
        cwd='/app',
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    flash('Configuration enregistrée et sauvegarde lancée.', 'success')
    return redirect(url_for('index'))


@app.post('/test-destination')
def test_destination():
    destination = request.form.get('destination', '').strip()
    try:
        from backup import translate_destination, validate_destination
        mapped = translate_destination(destination)
        validate_destination(destination, mapped, create=False)
        flash(f'Destination accessible : {destination}', 'success')
    except Exception as exc:
        flash(f'Destination non disponible : {exc}', 'error')
    return redirect(url_for('index'))


@app.post('/delete-backups')
def delete_backups():
    if backup_running():
        flash('Impossible de supprimer pendant une sauvegarde ou une restauration.', 'error')
        return redirect(url_for('index'))

    selected = sorted(set(request.form.getlist('snapshots')))
    if not selected:
        flash('Aucune sauvegarde sélectionnée.', 'error')
        return redirect(url_for('index'))

    if request.form.get('confirm_delete') != 'on':
        flash('Confirme la suppression avant de continuer.', 'error')
        return redirect(url_for('index'))

    s = get_settings()
    try:
        base = mapped_backup_base(s)
        if not base.is_dir():
            raise RuntimeError(f'Destination de sauvegarde introuvable : {s.get("destination")}')
    except Exception as exc:
        flash(f'Impossible d’accéder aux sauvegardes : {exc}', 'error')
        return redirect(url_for('index'))

    deleted = []
    missing = []
    errors = []

    for snapshot in selected:
        if not TIMESTAMP_RE.match(snapshot):
            errors.append(f'{snapshot} : nom de snapshot invalide')
            continue
        target = base / snapshot
        try:
            # Protection supplémentaire : le chemin supprimé doit être un enfant direct
            # de la destination configurée et respecter le format horodaté.
            if target.parent.resolve() != base.resolve():
                raise RuntimeError('chemin hors destination')
            if target.is_dir():
                shutil.rmtree(target)
                deleted.append(snapshot)
            else:
                missing.append(snapshot)
        except Exception as exc:
            errors.append(f'{snapshot} : {exc}')

    # L'historique de sauvegarde correspondant est nettoyé seulement pour les snapshots
    # réellement supprimés ou déjà absents. Les journaux de restauration restent conservés.
    cleaned = deleted + missing
    if cleaned:
        with db() as conn:
            conn.executemany('DELETE FROM backups WHERE snapshot=?', [(name,) for name in cleaned])

    if deleted:
        flash(f'{len(deleted)} sauvegarde(s) supprimée(s).', 'success')
    if missing:
        flash(f'{len(missing)} entrée(s) sans dossier ont été retirées de l’historique.', 'success')
    if errors:
        flash('Certaines suppressions ont échoué : ' + ' | '.join(errors[:5]), 'error')
    return redirect(url_for('index'))


@app.get('/log/<int:backup_id>')
def show_log(backup_id):
    with db() as conn:
        row = conn.execute('SELECT * FROM backups WHERE id=?', (backup_id,)).fetchone()
    if not row:
        return 'Sauvegarde introuvable', 404
    text = ''
    if row['log_file'] and Path(row['log_file']).exists():
        text = Path(row['log_file']).read_text(errors='replace')[-100000:]
    return render_template('log.html', backup=row, text=text)


@app.get('/restore')
def restore_page():
    s = get_settings()
    snapshots, snapshot_error = snapshot_list(s)
    requested = request.args.get('snapshot', '').strip()
    selected_snapshot = requested or (snapshots[0]['name'] if snapshots else '')
    manifest = None
    containers = []
    detail_error = None
    if selected_snapshot:
        try:
            _, manifest, containers = snapshot_detail(s, selected_snapshot)
        except Exception as exc:
            detail_error = str(exc)
    with db() as conn:
        restores = conn.execute('SELECT * FROM restores ORDER BY id DESC LIMIT 20').fetchall()
    return render_template(
        'restore.html',
        s=s,
        snapshots=snapshots,
        snapshot_error=snapshot_error,
        selected_snapshot=selected_snapshot,
        manifest=manifest,
        containers=containers,
        detail_error=detail_error,
        restores=restores,
        running=backup_running(),
        progress=read_progress(),
    )


@app.post('/restore-run')
def restore_run():
    if backup_running():
        flash('Une sauvegarde ou une restauration est déjà en cours.', 'error')
        return redirect(url_for('restore_page'))
    if request.form.get('confirm_restore') != 'on':
        flash('Coche la confirmation avant de lancer une restauration.', 'error')
        return redirect(url_for('restore_page', snapshot=request.form.get('snapshot', '')))

    snapshot = request.form.get('snapshot', '').strip()
    action = request.form.get('action', 'files')
    if action not in {'files', 'full'}:
        action = 'files'
    selected = sorted(set(request.form.getlist('selected_containers')))
    if not selected:
        flash('Aucun conteneur sélectionné.', 'error')
        return redirect(url_for('restore_page', snapshot=snapshot))
    if SELF_NAME in selected:
        flash('Le Backup Manager ne peut pas se restaurer lui-même pendant son exécution.', 'error')
        return redirect(url_for('restore_page', snapshot=snapshot))

    try:
        s = get_settings()
        _, _, rows = snapshot_detail(s, snapshot)
        available = {r['name'] for r in rows}
        if any(name not in available for name in selected):
            raise ValueError('La sélection contient un conteneur absent de cette sauvegarde.')
    except Exception as exc:
        flash(str(exc), 'error')
        return redirect(url_for('restore_page', snapshot=snapshot))

    cmd = ['/usr/local/bin/python', '/app/restore.py', '--snapshot', snapshot, '--action', action]
    for name in selected:
        cmd += ['--container', name]
    subprocess.Popen(
        cmd,
        cwd='/app',
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    if action == 'full':
        flash('Restauration + reconstruction lancée.', 'success')
    else:
        flash('Restauration des fichiers lancée.', 'success')
    return redirect(url_for('restore_page', snapshot=snapshot))


@app.get('/restore-log/<int:restore_id>')
def show_restore_log(restore_id):
    with db() as conn:
        row = conn.execute('SELECT * FROM restores WHERE id=?', (restore_id,)).fetchone()
    if not row:
        return 'Restauration introuvable', 404
    text = ''
    if row['log_file'] and Path(row['log_file']).exists():
        text = Path(row['log_file']).read_text(errors='replace')[-100000:]
    return render_template('restore_log.html', restore=row, text=text)


@app.get('/api/status')
def api_status():
    with db() as conn:
        last = conn.execute('SELECT * FROM backups ORDER BY id DESC LIMIT 1').fetchone()
    return jsonify({
        'running': backup_running(),
        'last': dict(last) if last else None,
        'progress': read_progress(),
        'now': datetime.now().isoformat(timespec='seconds'),
    })


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--init-only', action='store_true')
    args = parser.parse_args()
    init_db()
    if not args.init_only:
        app.run(host='0.0.0.0', port=9876, debug=False)
else:
    init_db()
