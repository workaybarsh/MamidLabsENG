"""MamidLabs local speech + audio-event transcription pipeline.

Modules:
    config   profile by GPU VRAM (LITE/PC1 vs PRO/PC2)
    audio    convert to 16 kHz mono WAV + chunking
    vad      Silero VAD speech ranges
    asr      faster-whisper (Whisper large-v3), hallucination filters
    events   AudioSet/AST audio-event labels
    diarize  pyannote speaker diarization (optional)
    merge    chronological merge -> text
    main     runs the pipeline; writes txt + json
"""
__version__ = '1.0.0'
