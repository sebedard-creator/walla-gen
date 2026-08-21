# Changelog — walla-gen

## 2026-08-21

### Ajouté

- Correction de la transcription d'une voix sauvegardée depuis l'interface.
- Suppression définitive d'une voix et de son audio avec dialogue de confirmation.
- Trois onglets cliquables pour naviguer librement entre scénario, script et audio.
- Génération audio suivie en arrière-plan et bouton **Abort** qui demande l'annulation réelle de la prédiction Replicate.

### Modifié

- Interface mise à niveau en **v2.0** : palette chaude à faible éblouissement, hiérarchie visuelle simplifiée et présentation adaptée aux écrans mobiles.

### Retiré

- Toute l'intégration Pro Tools/PTX, les routes associées, la dépendance `pt_api` et les paramètres de template.

## 2026-08-08

### Ajouté

- Nettoyage léger et temporaire des références de clonage avec `ffmpeg`.
- Option **Accent québécois renforcé** dans l'interface française.

### Modifié

- La réécriture québécoise avant TTS reste disponible, mais elle est désactivée par défaut afin de préserver chaque mot du script affiché.
