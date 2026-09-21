# Handoff — walla-gen

État au 21 août 2026.

Lancement local :

```powershell
.\.venv\Scripts\python.exe app.py
```

La configuration minimale demande `ANTHROPIC_API_KEY` et `REPLICATE_API_KEY` dans `.env`. `ffmpeg` doit être accessible via `PATH` ou `FFMPEG_EXECUTABLE`.

Fonctions principales : génération de script, transcription de référence, normalisation de niveau sans débruitage, clonage Qwen3-TTS, bibliothèque de voix locale avec correction/suppression, trois onglets cliquables et annulation des générations en cours.

Après toute modification de `.env` ou `app.py`, redémarrer le service. Ne pas supprimer `voice_library/` ni `certs/` pendant que le service est en cours d'utilisation.
