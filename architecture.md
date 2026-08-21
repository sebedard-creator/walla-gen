# Architecture — walla-gen

État vérifié le 21 août 2026 — interface v2.0.

## Vue d'ensemble

`walla-gen` est une application Flask locale. `app.py` contient le backend et `templates/index.html` l'interface web française. Le programme génère des scripts, clone une voix de référence avec Qwen3-TTS via Replicate et télécharge un WAV.

Le processus peut servir HTTP et HTTPS simultanément : HTTP dans un thread Werkzeug et HTTPS dans le thread principal. Les deux partagent la même bibliothèque de voix locale.

## Interface

L'interface v2.0 contient une palette chaude à faible éblouissement et trois onglets cliquables :

1. **Scénario** : langue, scénario, microphone, référence audio, bibliothèque et transcription.
2. **Script** : édition du texte, direction de jeu et options de préparation de la référence.
3. **Audio** : état de la génération, téléchargement du WAV et bouton **Abort** pendant une génération active.

La bibliothèque est filtrée dans le navigateur : `FRAN` en français, `ENG` en anglais et les noms sans préfixe dans les deux langues. Une voix sauvegardée peut être écoutée, sa transcription peut être corrigée et elle peut être supprimée définitivement après confirmation.

## Génération audio

1. `POST /generate-audio` valide la requête, conserve temporairement un éventuel téléversement et démarre une tâche en arrière-plan.
2. La tâche prépare une copie temporaire mono 24 kHz de la référence quand `REFERENCE_AUDIO_CLEANUP=true`. L'original, y compris dans `voice_library/`, ne change jamais.
3. Le script est nettoyé des didascalies. `QUEBEC_TTS_REWRITE` reste désactivé par défaut afin de transmettre exactement le texte affiché.
4. Une prédiction Replicate asynchrone est créée pour Qwen3-TTS, puis suivie par `GET /audio-jobs/<job_id>`.
5. `POST /audio-jobs/<job_id>/abort` demande l'annulation à Replicate. La tâche cesse aussi si elle est encore en attente locale.
6. À succès, le WAV est téléchargé dans `cache/output/` et offert par `GET /audio-jobs/<job_id>/download`.

Les appels Replicate sont espacés selon `REPLICATE_MIN_SECONDS_BETWEEN_CALLS`.

## Routes

| Méthode | Route | Rôle |
|---|---|---|
| GET | `/` | Interface principale. |
| POST | `/clear-cache` | Vide le cache et les diagnostics. |
| POST | `/mic-test` | Transcrit une dictée micro. |
| GET / POST | `/voice-library` | Liste ou sauvegarde des voix. |
| PATCH / DELETE | `/voice-library/<voice_id>` | Corrige la transcription ou supprime une voix et son audio. |
| GET | `/voice-library/<voice_id>/audio` | Lit l'audio d'une voix sauvegardée. |
| POST | `/generate-script` | Génère le script. |
| POST | `/generate-direction` | Génère la direction de jeu. |
| POST | `/transcribe-reference` | Transcrit la référence. |
| POST | `/generate-audio` | Démarre la génération asynchrone. |
| GET | `/audio-jobs/<job_id>` | Retourne l'état d'une génération. |
| POST | `/audio-jobs/<job_id>/abort` | Annule une génération active. |
| GET | `/audio-jobs/<job_id>/download` | Télécharge le WAV terminé. |
| GET | `/local-ca.crt` | Télécharge l'autorité HTTPS publique. |

## Données locales et sécurité

- `voice_library/` contient les voix et `index.json`.
- `cache/input/` et `cache/output/` ne contiennent que des fichiers temporaires.
- Les dernières transcription et charge de génération sont dans les fichiers de diagnostic à la racine.
- `.env`, `voice_library/`, `cache/`, `certs/` et les diagnostics sont ignorés par Git.
- L'application ne doit rester accessible que sur un LAN de confiance : elle n'a pas de comptes ni d'authentification.
