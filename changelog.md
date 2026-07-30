# Changelog — walla-gen

## 2026-07-22

### Added

- Pro Tools walla workflow based on `pt_api` 1.4.0 and its new read-only `get_timeline_clip_groups()` method.
- Clip Group validation for labels in the compact `F|A F|H scénario` format, including track, duration and compatible-library-voice checks.
- Automatic random selection of a matching saved voice, walla-oriented script generation, automatic performance direction, and one TTS render per valid Clip Group.
- PTX session generation from a prepared `pt_api` template (empty timeline, retained imported prototype), with source positions preserved, a self-contained `Audio Files` folder, and `WALLA_MANIFEST.json`.
- Strict BWF WAV rendering through configurable `ffmpeg`, validated against `pt_api` before session construction.
- Browser controls to inspect the PTX plan and download the final session ZIP.

### Changed

- `.env.example` now documents `PT_API_PATH`, `FFMPEG_EXECUTABLE`, `WALLA_SLOT_MAX_SECONDS` and `WALLA_TEMPLATE_PATH`.
- README and architecture documentation describe the template contract and walla delivery workflow.
- The voice selector marks saved voices used successfully during the current page session; the marker intentionally resets after a refresh.

## 2026-07-16

### Ajouté

- HTTPS local sur le port 5051, servi en parallèle d'HTTP 5050 par le même processus.
- `setup_https.py` pour générer une autorité locale et un certificat serveur adaptés à l'adresse LAN.
- Route `GET /local-ca.crt` pour distribuer uniquement le certificat public de l'autorité.
- Dépendance `cryptography>=42.0.0`, utilisée par le générateur de certificats.

### Modifié

- La bibliothèque de voix est filtrée selon la langue sélectionnée : préfixe `FRAN` en français, `ENG` en anglais, et voix sans ces préfixes dans les deux langues.
- Une voix préfixée incompatible est désélectionnée lors du changement de langue.
- `.env.example` décrit `HTTPS_PORT`, `WALLA_SSL_CERT_FILE` et `WALLA_SSL_KEY_FILE`.
- `certs/` est ignoré par Git afin de protéger la clé privée et les certificats propres à la machine.
- README, architecture, audit et handoff ont été réécrits pour correspondre au code et au déploiement actuels.

### Maintenance

- Cache audio, diagnostics et bytecode Python temporaire nettoyés après validation; la bibliothèque de voix et les certificats ont été conservés.
- Les anciens utilitaires pare-feu `.bat` ne font plus partie de l'état actuel; les commandes `netsh` administrateur sont documentées dans le README.

## 2026-07-06

### Supprimé à cette date

- Ancienne chaîne Azure TTS, fallback Whisper et branches CosyVoice/Chatterbox.
- Anciens scripts de démarrage HTTP/HTTPS et ancien dossier de certificats.
- Anciennes variables Azure, Chatterbox et fournisseur TTS.
- Dépendance `cryptography`, car le mode HTTPS de l'époque avait été abandonné.

Le mode HTTPS et `cryptography` ont ensuite été réintroduits le 16 juillet 2026 avec une nouvelle implémentation; cette entrée décrit bien l'état historique du 6 juillet, pas l'état actuel.

## 2026-06-28

### Ajouté

- `.gitignore` pour `.env`, `.venv/`, `cache/`, `voice_library/`, les journaux et les diagnostics.
- Première version de `architecture.md`.

### Modifié

- Configuration de `logging.basicConfig` avec `stream=sys.stdout`, niveau `INFO` et `force=True` afin que les logs Flask/Werkzeug de routine ne soient pas classés comme erreurs par PyManager.
