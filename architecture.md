# Architecture — walla-gen

État vérifié le 22 juillet 2026.

## Vue d'ensemble

L'application est une SPA légère servie par Flask. Tout le backend se trouve dans `app.py`; toute l'interface se trouve dans `templates/index.html`.

Le processus principal charge `.env`, initialise l'application Flask et choisit l'un des deux modes suivants :

- sans `HTTPS_PORT` : serveur de développement Flask sur `HOST:PORT`;
- avec `HTTPS_PORT` : deux serveurs Werkzeug dans le même processus, HTTP sur `PORT` et HTTPS sur `HTTPS_PORT`. Le serveur HTTP tourne dans un thread et le serveur HTTPS dans le thread principal.

Cette architecture évite deux processus concurrents qui modifieraient la même bibliothèque de voix. Les verrous `voice_library_lock` et `replicate_lock` ne protègent que les accès effectués dans ce processus unique.

## Composants

### Interface

`templates/index.html` contient :

- la mise en page et les styles;
- le choix Français/Anglais;
- la capture micro avec `getUserMedia` et `MediaRecorder`;
- les appels `fetch` vers Flask;
- le filtrage client de la bibliothèque selon `FRAN` et `ENG`;
- le marquage visuel en mémoire des voix de bibliothèque déjà utilisées durant la session de page;
- la lecture préalable d'une voix;
- la copie du script et le téléchargement du WAV.
- le flux **Pro Tools · Walla automatique** : import du PTX portant les Clip Groups, utilisation de `walla_template.ptx`, vérification des slots et téléchargement de l'archive PTX finale.

Les polices Google sont chargées depuis `fonts.googleapis.com`; l'application reste fonctionnelle sans elles grâce aux polices de repli.

### Backend Flask

`app.py` assure :

- la validation des requêtes;
- l'accès Anthropic et Replicate;
- la préparation du texte et des instructions Qwen3-TTS;
- la persistance de la bibliothèque de voix;
- la gestion du cache;
- l'import local de `pt_api` 1.4.0, la lecture des occurrences de Clip Groups et la construction d'une session PTX depuis une template;
- la conversion des sorties TTS avec `ffmpeg` en WAV BWF mono 48 kHz/float compatible avec le builder de `pt_api`;
- le service HTTP/HTTPS et le téléchargement de l'autorité locale.

### Services externes

- Anthropic, modèle `claude-haiku-4-5-20251001` : génération du script, direction de jeu et réécriture québécoise optionnelle avant TTS.
- Replicate, modèle configurable par `REPLICATE_TRANSCRIBE_MODEL` : transcription.
- Replicate, modèle configurable par `REPLICATE_TTS_MODEL` : clonage vocal Qwen3-TTS.
- `requests` : téléchargement du fichier audio produit par l'URL retournée par Replicate.
- `pt_api` 1.4.0 : lecture en seule lecture de `get_timeline_clip_groups()` et `build_audio_session()`.
- `ffmpeg` : conversion/rééchantillonnage avant l'écriture BWF.

## Flux principaux

### Script et direction

1. Le navigateur envoie le scénario, la durée et la langue à `/generate-script`.
2. Anthropic retourne uniquement le script.
3. `/generate-direction` produit une phrase de direction de 18 mots maximum.

### Transcription

1. `/transcribe-reference` reçoit un fichier ou l'identifiant d'une voix sauvegardée.
2. Le backend transmet l'audio et la langue à Replicate.
3. Le résultat est normalisé et écrit dans `last-transcription-debug.txt`.

`/mic-test` utilise le même modèle mais force actuellement la langue française et conserve l'enregistrement reçu dans `cache/input/` jusqu'au prochain nettoyage.

### Génération audio

1. `/generate-audio` reçoit le script, la transcription exacte, la direction, la langue et une référence audio ou un identifiant de bibliothèque.
2. En français seulement, `QUEBEC_TTS_REWRITE=true` déclenche une réécriture Anthropic avant le TTS. La configuration locale d'exemple utilise `false`.
3. Les didascalies courtes entre astérisques, crochets ou parenthèses sont retirées.
4. Le backend construit une entrée Qwen3-TTS en mode `voice_clone`.
5. Les appels Replicate sont espacés selon `REPLICATE_MIN_SECONDS_BETWEEN_CALLS`.
6. Le WAV distant est téléchargé dans `cache/output/`, puis envoyé au navigateur.

### Session walla Pro Tools

1. `/walla/inspect` reçoit le PTX qui porte les Clip Groups et utilise la template définie par `WALLA_TEMPLATE_PATH` (par défaut `walla_template.ptx`). Il lit `get_timeline_clip_groups()`, valide le format compact `F|A F|H scénario`, trouve les voix compatibles, vérifie les pistes et appelle le validateur de template de `pt_api` sans modifier le PTX. Il ne fait aucun appel Anthropic/Replicate.
2. `/walla/generate-session` refait cette validation avant toute dépense, choisit une voix compatible aléatoirement pour chaque slot, génère script, direction et TTS, puis convertit chaque rendu en BWF valide.
3. `build_audio_session()` crée une nouvelle session depuis la template : chaque rendu vise la piste du Clip Group et sa position `start_samples`.
4. Le serveur ajoute `WALLA_MANIFEST.json`, archive le dossier de session (PTX et `Audio Files`) puis retourne le ZIP. Les sessions et archives restent dans `cache/output/` jusqu'au nettoyage.

## Routes

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/` | Interface principale. |
| POST | `/clear-cache` | Vide `cache/input`, `cache/output` et les diagnostics. |
| POST | `/mic-test` | Enregistre/transcrit la dictée du scénario en français. |
| GET | `/voice-library` | Retourne toutes les voix valides, triées par nom. |
| POST | `/voice-library` | Sauvegarde une voix et sa transcription. |
| GET | `/voice-library/<voice_id>/audio` | Sert l'audio d'une voix sauvegardée. |
| GET | `/local-ca.crt` | Télécharge l'autorité HTTPS publique. |
| POST | `/generate-script` | Génère un script français ou anglais. |
| POST | `/generate-direction` | Génère la direction de jeu. |
| POST | `/transcribe-reference` | Transcrit une référence selon la langue choisie. |
| POST | `/generate-audio` | Génère et retourne le WAV cloné. |
| POST | `/walla/inspect` | Valide les Clip Groups et les voix compatibles, sans génération. |
| POST | `/walla/generate-session` | Génère les voix et retourne un ZIP de session PTX autonome. |

Le filtre `FRAN`/`ENG` n'est pas appliqué par `/voice-library`; la route retourne toutes les voix et `voiceMatchesLanguage()` filtre les options dans le navigateur.

## Persistance

- `voice_library/index.json` contient une liste d'objets `id`, `name`, `filename`, `transcript`, `created_at`.
- Les fichiers audio sauvegardés se trouvent à côté de l'index.
- Les entrées dont le fichier manque ou dont le chemin est invalide sont retirées de l'index lors du chargement de la bibliothèque.
- L'index est écrit via `index.tmp`, puis remplacé atomiquement.
- `cache/input/` contient les entrées temporaires et `cache/output/` les générations.
- Les PTX envoyés, rendus intermédiaires, sessions PTX finales et ZIP walla sont temporaires : ils sont tous sous `cache/` et donc supprimés par **Clear Cache**.
- Les diagnostics contiennent les dernières transcription et charge de génération; ils peuvent inclure le texte fourni par l'utilisateur.

## HTTPS

`setup_https.py`, qui dépend de `cryptography`, génère une autorité locale et un certificat serveur RSA. La clé de l'autorité n'est pas conservée après la génération. Le serveur utilise :

- `HTTPS_PORT`;
- `WALLA_SSL_CERT_FILE`;
- `WALLA_SSL_KEY_FILE`.

Les chemins relatifs sont résolus depuis la racine de l'application. Si `HTTPS_PORT` est défini et que le certificat ou la clé manque, le démarrage échoue explicitement.

Les certificats générés couvrent l'adresse LAN détectée, `127.0.0.1` et `localhost`. Le nom de l'ordinateur est volontairement exclu. `localhost` fonctionne seulement sur le PC serveur; les autres appareils doivent utiliser l'adresse LAN.

## Sécurité et portée

- Taille maximale des requêtes Flask : 50 Mo.
- Les chemins du cache et de la bibliothèque sont validés avant lecture ou suppression.
- `.env`, `certs/`, `voice_library/`, `cache/` et les diagnostics sont ignorés par Git.
- Le certificat de l'autorité est public; `walla-server.key` est privé.
- Il n'y a pas d'authentification, de comptes, de contrôle d'accès par route ni de protection CSRF explicite.
- Les serveurs sont liés à `0.0.0.0` dans la configuration actuelle. L'application doit rester sur un LAN de confiance et ne doit pas être exposée directement à Internet.
