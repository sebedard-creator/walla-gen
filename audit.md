# Audit du projet — walla-gen

Date de vérification : 22 juillet 2026.

Portée : cohérence du code, dépendances, routes, fichiers, HTTPS, persistance et documentation.

## Verdict actuel

Le pipeline actif est cohérent : Anthropic génère les textes et Replicate assure transcription et clonage Qwen3-TTS. Azure Speech, CosyVoice, Chatterbox et Whisper ont bien été retirés. HTTPS est de nouveau actif et n'est plus du code mort. Le flux Pro Tools s'appuie sur `pt_api` 1.4.0 pour lire les placements de Clip Groups et créer des sessions depuis une template.

Les cinq documents Markdown ont été alignés sur l'état actuel. Les affirmations précédentes selon lesquelles HTTPS, `certs/`, le port 5051 et `cryptography` étaient obsolètes ne décrivaient plus la réalité après les changements du 16 juillet.

## Dépendances

| Dépendance | Usage actuel |
|---|---|
| `flask>=3.0.0` | Application, routes, réponses et serveurs Werkzeug. |
| `anthropic>=0.40.0` | Scripts, directions et réécriture québécoise optionnelle. |
| `replicate>=0.34.0` | Transcription et Qwen3-TTS. |
| `python-dotenv>=1.0.0` | Chargement de `.env`. |
| `requests>=2.31.0` | Téléchargement du WAV retourné par Replicate. |
| `cryptography>=42.0.0` | Génération des certificats par `setup_https.py`. |
| `pt_api` 1.4.0 (checkout local) | Lecture des Clip Groups et construction de session PTX. |
| `ffmpeg` (exécutable externe) | Conversion TTS en BWF 48 kHz/float compatible `pt_api`. |

Aucune dépendance déclarée n'est orpheline à l'échelle du projet. `cryptography` n'est pas importé par le serveur après le démarrage, mais il est requis pour créer ou renouveler les certificats.

## Fonctions et routes

- Toutes les fonctions utilitaires définies dans `app.py` sont appelées.
- Les fonctions de routes apparaissent une seule fois dans une recherche statique parce qu'elles sont appelées par Flask.
- Toutes les routes utilisées par `templates/index.html` existent.
- La route supplémentaire `/local-ca.crt` est destinée à l'installation de la confiance HTTPS.
- Les routes `/walla/inspect` et `/walla/generate-session` ont une interface correspondante dans `templates/index.html`.
- La conversion BWF de walla-gen a été testée avec le validateur strict de `pt_api`; les 189 tests automatisés de `pt_api` 1.4.0 passent dans l'environnement de walla-gen.
- Aucun code Azure, CosyVoice, Chatterbox ou Whisper n'est encore présent.

## Constats ouverts

### Priorité moyenne — contrôle d'accès LAN

Le serveur écoute sur `0.0.0.0` et ne possède ni authentification ni protection CSRF explicite. Un appareil pouvant joindre le serveur peut notamment appeler la génération, sauvegarder une voix ou vider le cache. Cette conception est acceptable seulement sur un LAN de confiance. Ne pas publier les ports sur Internet.

Le même constat couvre `/walla/generate-session`, qui peut déclencher plusieurs appels Anthropic/Replicate en une requête. L'interface demande une confirmation et la vérification préalable est sans coût, mais il n'existe pas de contrôle d'accès côté serveur.

### Priorité moyenne — langue de la dictée micro

`/mic-test` appelle `run_transcription(..., 'fr')` et le navigateur n'envoie pas la langue sélectionnée à cette route. La dictée du scénario est donc toujours guidée comme du français. `/transcribe-reference` respecte correctement Français/Anglais.

### Priorité faible — contrat JSON

`generate_script()` utilise directement `request.get_json()` puis `.get()`. L'interface envoie toujours un objet JSON, mais un client externe qui envoie un corps absent ou invalide peut provoquer une réponse 500 au lieu d'une erreur 400.

### Priorité faible — imports inutilisés

`from html import escape` et `import xml.etree.ElementTree as ET` ne sont référencés nulle part après leur import. Ils n'altèrent pas le fonctionnement mais peuvent être supprimés lors d'un prochain nettoyage de code.

### Information — serveur local

HTTP et HTTPS utilisent Werkzeug. C'est adapté à l'usage local prévu, mais ce n'est pas une configuration de production Internet avec reverse proxy, authentification, supervision TLS et limitation de débit par utilisateur.

### Information — certificat lié à l'adresse

Le certificat contient l'adresse LAN détectée au moment de sa création. Un changement d'adresse exige une régénération avec `setup_https.py --force` et l'installation de la nouvelle autorité sur les clients. `localhost` ne permet un accès que depuis le PC serveur.

## Persistance et fichiers

- `safe_cache_path()` empêche le nettoyage hors de `cache/`.
- `voice_audio_path()` empêche la lecture d'un chemin sortant de `voice_library/`.
- L'écriture de l'index de voix utilise un fichier temporaire et `os.replace()`.
- Le chargement retire de l'index les voix dont le fichier est absent ou le chemin invalide.
- `Clear Cache` ne touche pas à `voice_library/` ni à `certs/`.
- `Clear Cache` supprime les PTX importés, rendus intermédiaires, sessions et ZIP walla, car ils résident tous dans `cache/`.
- `.gitignore` couvre `.env`, `.venv/`, `__pycache__/`, `cache/`, `voice_library/`, `certs/`, les logs, XML et TXT, avec exceptions pour `requirements.txt` et `README.md`.

## HTTPS vérifié

- HTTP : écoute sur `0.0.0.0:5050`.
- HTTPS : écoute sur `0.0.0.0:5051` dans le même processus.
- Le certificat serveur couvre l'adresse LAN détectée, `127.0.0.1` et `localhost`; le nom de l'ordinateur est volontairement exclu.
- La clé de l'autorité n'est pas conservée par `setup_https.py`; la clé privée du serveur demeure dans `certs/walla-server.key`.
- L'autorité publique est disponible sur `/local-ca.crt`.
- Aucun script pare-feu `.bat` ni règle de port Windows dédiée 5050/5051 n'était présent lors de la vérification; les commandes manuelles sont consignées dans le README.

## Cohérence de l'interface

- Français est la langue par défaut.
- `voiceMatchesLanguage()` filtre les noms de façon insensible à la casse après suppression des espaces initiaux et finaux.
- Les voix commençant par `FRAN` ou `ENG` sont réservées à leur langue; les autres restent visibles partout.
- Le backend retourne volontairement toute la bibliothèque; le filtrage est seulement visuel.
- Une sélection incompatible est vidée lors du changement de langue, avec sa transcription.

## Conclusion

La documentation décrit maintenant le code et le déploiement observés. Les écarts fonctionnels connus sont consignés ci-dessus plutôt que présentés comme des fonctions déjà corrigées.
