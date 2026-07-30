# Handoff — walla-gen

État vérifié le 22 juillet 2026.

## État opérationnel

- PyManager lance automatiquement le Python de l'environnement virtuel avec `app.py` depuis le dossier du projet.
- Un seul processus écoute actuellement sur `0.0.0.0:5050` en HTTP et `0.0.0.0:5051` en HTTPS.
- Les tests locaux des accès HTTP, HTTPS et `/local-ca.crt` ont retourné HTTP 200.
- L'autorité locale est installée dans le magasin de certificats de l'utilisateur Windows actuel.
- Le cache et les diagnostics ont été vidés après les tests; la bibliothèque de 39 voix a été conservée.
- `pt_api` 1.4.0 est détecté automatiquement depuis le dossier frère `../pt_api` (ou via `PT_API_PATH`) et expose la lecture des placements de Clip Groups utilisée par walla-gen.

## Architecture active

- Flask/Werkzeug : interface et API locale.
- Anthropic `claude-haiku-4-5-20251001` : scripts, directions et réécriture québécoise optionnelle.
- Replicate Qwen3-TTS : clonage vocal.
- Replicate `openai/gpt-4o-mini-transcribe` : transcription.
- PyManager : démarrage et supervision sur la machine actuelle.
- `cryptography` et `setup_https.py` : génération/renouvellement des certificats locaux.
- `pt_api` 1.4.0 : lecture des Clip Groups et construction PTX depuis une template.
- `ffmpeg` : conversion du TTS en BWF float mono 48 kHz avant l'export PTX.

Azure Speech, CosyVoice, Chatterbox et Whisper ne sont plus utilisés.

## Changements récents

- Filtrage visuel des voix par langue : `FRAN` en français, `ENG` en anglais, voix sans ces préfixes toujours visibles.
- Désélection automatique d'une voix préfixée devenue incompatible lors d'un changement de langue.
- Rétablissement d'HTTPS sur 5051 en parallèle d'HTTP sur 5050 dans le même processus.
- Génération d'une autorité locale et d'un certificat couvrant l'adresse LAN détectée, `127.0.0.1` et `localhost`, sans inclure le nom de l'ordinateur.
- Route `/local-ca.crt` pour installer la confiance sur les clients.
- Mise à jour complète des fichiers Markdown selon le code actuel.
- Ajout du flux walla Pro Tools : validation des groupes `F|A F|H scénario`, choix aléatoire d'une voix de bibliothèque compatible, génération par slot et archive ZIP de session PTX.

## Fichiers HTTPS à conserver

- `setup_https.py`
- `certs/walla-local-ca.crt`
- `certs/walla-server.crt`
- `certs/walla-server.key`

Le dossier `certs/` est ignoré par Git. Ne jamais transmettre `walla-server.key` à un appareil client. Seul `walla-local-ca.crt` doit être installé comme autorité de confiance.

Une modification de l'adresse IP nécessite une régénération avec `setup_https.py --force`, puis l'installation de la nouvelle autorité sur les clients. `localhost` fonctionne seulement sur le PC serveur; les autres appareils doivent utiliser l'adresse LAN.

## Intervention manuelle possible

Les anciens scripts pare-feu `.bat` ne sont plus présents et aucune règle de port dédiée 5050/5051 n'a été détectée pendant la vérification. Si un autre appareil ne rejoint pas le serveur, exécuter ces commandes dans un terminal administrateur Windows :

```text
netsh advfirewall firewall add rule name="walla-gen HTTP 5050" dir=in action=allow protocol=TCP localport=5050 profile=private,domain
netsh advfirewall firewall add rule name="walla-gen HTTPS 5051" dir=in action=allow protocol=TCP localport=5051 profile=private,domain
```

Le réseau Ethernet actif était de catégorie privée au moment de la vérification.

## Limites connues

- La dictée micro `/mic-test` transcrit toujours en français, même si Anglais est sélectionné.
- Le filtre `FRAN`/`ENG` est appliqué dans l'interface, pas dans l'API `/voice-library`.
- La sélection automatique walla exige aussi un sexe lisible dans le nom d'une voix (`Female`/`Male` ou équivalent). Les voix sans sexe explicite restent utilisables manuellement mais ne sont pas candidates à l'automatisation.
- Le builder `pt_api` impose une template 48 kHz / 23,976 avec pistes existantes, timeline visible et cachée vide, et un prototype audio importé. Une session de production réelle doit être validée avec cette template avant la première livraison.
- La durée visée est celle du Clip Group, mais le TTS n'est ni time-stretché ni tronqué automatiquement. Le manifeste permet de vérifier chaque rendu après coup.
- Le serveur Werkzeug et l'absence d'authentification conviennent au LAN de confiance, pas à une exposition Internet.
- `generate_script()` suppose que le corps de la requête est un objet JSON; l'interface respecte ce contrat, mais une requête externe sans JSON peut produire une erreur serveur.
- Les imports `escape` et `xml.etree.ElementTree as ET` sont inutilisés dans `app.py`; ils sont sans effet fonctionnel.

## Vérifications recommandées après une modification

```powershell
.\.venv\Scripts\python.exe -m py_compile app.py setup_https.py
curl.exe http://localhost:5050/
curl.exe --ssl-no-revoke https://localhost:5051/
```

Tester ensuite dans un appareil client ayant installé `walla-local-ca.crt` : chargement HTTPS, permission micro, transcription d'une référence et génération Qwen3-TTS complète. Pour le flux Pro Tools, placer une template valide dans `walla_template.ptx`, vérifier le PTX de Clip Groups avec **Importer et vérifier les Clip Groups**, puis lancer une session test d'un seul groupe et ouvrir le PTX produit dans Pro Tools.
