# Audit — walla-gen

État vérifié le 21 août 2026.

Le pipeline actif est limité à Anthropic pour les scripts et directions, Replicate pour la transcription et Qwen3-TTS, ainsi qu'à `ffmpeg` pour le nettoyage temporaire des références. Il n'y a plus d'intégration Pro Tools/PTX ni de dépendance `pt_api`.

La bibliothèque de voix reste locale et ignorée par Git. Ses routes permettent de lister, sauvegarder, corriger la transcription, écouter et supprimer une voix. La suppression retire l'entrée de l'index et le fichier audio après confirmation explicite dans l'interface.

Les générations audio sont des tâches asynchrones suivies par identifiant. Le bouton **Abort** appelle l'annulation de prédiction Replicate lorsque celle-ci est active. Les travaux restés en attente locale s'arrêtent avant la création de la prédiction.

Le programme est destiné à un LAN de confiance. Il n'a ni authentification ni contrôle d'accès par utilisateur; les ports HTTP/HTTPS ne doivent pas être exposés à Internet.
