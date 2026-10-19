"""MamidLabs local speech transcription pipeline.

Modules:
    config   tier = selected profile (lite/pro); VRAM is only the fallback (select_profile_for/select_profile)
    audio    convert to 16 kHz mono WAV + chunking
    vad      Silero VAD speech ranges
    asr      faster-whisper (Whisper large-v3), hallucination filters
    diarize  pyannote speaker diarization (optional)
    merge    chronological merge -> text
    main     runs the pipeline; writes txt + json
"""
# The PACKAGE release of agent/transcriber, independent of the application's version:
# the app's single source of version is MyAppVersion in packaging/windows/mamidlabs.iss
# (the release tag and the updater use that value). Bumping this package version is not
# an application release.
__version__ = '1.0.0'
