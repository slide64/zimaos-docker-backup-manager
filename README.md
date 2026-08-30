# ZimaOS Docker Backup Manager 0.1.7

Outil Web léger pour sauvegarder et reconstruire les conteneurs Docker sous ZimaOS après une panne.

## Sauvegarde

- sélection des conteneurs dans un tableau à cocher ;
- destination par défaut `/media/zimaos-backup-docker` ;
- sauvegarde immédiate ou planifiée par cron ;
- rétention configurable ;
- progression et messages en direct ;
- sauvegarde des chemins persistants sous `/DATA/AppData` ;
- sauvegarde des Compose associés ;
- sauvegarde des volumes Docker nommés ;
- inventaire Docker complet (`inspect`, réseaux, images, volumes) ;
- scripts autonomes de secours dans chaque snapshot.

## Restauration Web (0.1.6+)

L'onglet **Restauration** permet de :

1. choisir un snapshot ;
2. choisir les conteneurs à restaurer ;
3. lancer **Restaurer les fichiers** ou **Restaurer + reconstruire** ;
4. suivre la progression et les messages en direct ;
5. consulter l'historique et le journal des restaurations.

### Restaurer les fichiers

Restaure AppData, les Compose ZimaOS et les volumes Docker, sans recréer les conteneurs.

### Restaurer + reconstruire

Restaure les données puis recrée les conteneurs à partir de l'inventaire Docker sauvegardé : image, ports, variables d'environnement, mounts, restart policy et réseaux. Si une image n'existe plus localement, l'outil essaie de la télécharger. Pour une image construite localement, il tente un rebuild depuis le projet Compose sauvegardé lorsque le contexte de build peut être déterminé.

Les montages externes (`/media`, `/mnt`, NAS, médias, téléchargements...) ne sont pas copiés. Ils doivent exister aux mêmes chemins avant la reconstruction. La restauration complète est bloquée si un chemin externe sous `/media`, `/mnt` ou `/DATA` requis est absent.

### Nextcloud AIO

Si `nextcloud-aio-mastercontainer` est sélectionné, les données des conteneurs enfants AIO sont restaurées mais ils ne sont pas recréés individuellement. Le mastercontainer reprend leur gestion.

## Organisation d'un snapshot

```text
20260829-181500/
├── containers/
│   ├── jellyfin/
│   │   ├── INFO.txt
│   │   ├── container.json
│   │   ├── container-inspect.json
│   │   ├── appdata/
│   │   ├── compose/
│   │   └── volumes.txt
│   └── ...
├── shared-volumes/
├── inventaire/
├── manifest.json
├── rebuild-plan.tsv
├── restore-zimaos.sh
└── rebuild-stacks.sh
```

## Installation / mise à jour

```bash
cd /tmp
tar -xzf /DATA/Documents/zimaos-docker-backup-manager-v0.1.7.tar.gz
cd zimaos-docker-backup-manager-v0.1.7
sudo ./install.sh
```

Le programme est installé dans :

```text
/DATA/AppData/zimaos-docker-backup-manager
```

Interface par défaut :

```text
http://IP_DU_ZIMAOS:9877
```

Une mise à jour conserve les réglages, la sélection des conteneurs, l'historique et le port existant.

## Secours sans interface

Chaque snapshot conserve toujours :

```bash
sudo ./restore-zimaos.sh
sudo ./rebuild-stacks.sh
```

Ces scripts restent une solution de secours indépendante de l'interface Web.
