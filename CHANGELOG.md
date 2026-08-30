# Changelog

## 0.1.7

- Ajout de la suppression manuelle des sauvegardes depuis l’historique.
- Sélection multiple avec Tout cocher / Tout décocher.
- Double confirmation avant suppression (case de confirmation + boîte de dialogue).
- Suppression du dossier snapshot et de son entrée dans l’historique.
- Les opérations en cours ne peuvent pas être supprimées.
- Les snapshots incomplets/échoués peuvent également être nettoyés.


## 0.1.6

- Ajout d'un écran **Restauration après crash**.
- Sélection du snapshot à restaurer.
- Tableau à cocher des conteneurs présents dans la sauvegarde.
- Mode **Restaurer les fichiers** : AppData + Compose + volumes Docker.
- Mode **Restaurer + reconstruire** : recréation automatique des conteneurs depuis `container-inspect.json`.
- Recréation des réseaux Docker nécessaires avant les conteneurs.
- Conservation des paramètres de runtime : image, environnement, ports, mounts, restart policy, labels et HostConfig sauvegardés.
- Téléchargement automatique des images absentes.
- Tentative de reconstruction des images locales depuis un projet Compose sauvegardé lorsque le contexte `build` est exploitable.
- Restauration des volumes Docker avec leurs métadonnées principales (driver, options, labels).
- Vérification préalable des bind mounts externes sous `/media`, `/mnt` et `/DATA`.
- Gestion spécifique Nextcloud AIO : le mastercontainer reprend la création de ses conteneurs enfants.
- Barre de progression et journal en direct également pour la restauration.
- Historique dédié des restaurations.
- Verrou unique empêchant sauvegarde et restauration simultanées.
- Les chemins Docker nécessaires à une restauration sont montés en écriture dans le Backup Manager.
- `requests` épinglé en 2.32.5 avec Docker SDK 7.1.0 pour éviter une régression connue de création de conteneurs avec requests 2.33.0.

## 0.1.5

- Ajout d'une barre de progression en direct dans l'interface.
- Nouvelle hiérarchie de sauvegarde organisée par nom de conteneur.
- Volumes Docker partagés stockés une seule fois dans `shared-volumes/`.
