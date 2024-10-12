import logging
from abc import ABC, abstractmethod
from enum import Enum
from pathlib import Path
from typing import List, Optional, Dict, Any, Callable, Tuple, Union, Coroutine
from pydub import AudioSegment as PydubAudioSegment
from functools import wraps
from concurrent.futures import ThreadPoolExecutor, as_completed
import asyncio
from contextlib import contextmanager


class PodcastState(Enum):
    """Enum representing the different states of a podcast during creation."""
    INITIALIZED = 0         # Initial state when the Podcast object is created
    TRANSCRIPT_BUILT = 1    # State after the transcript has been generated
    AUDIO_SEGMENTS_BUILT = 2  # State after individual audio segments have been created
    STITCHED = 3            # Final state after all audio segments have been combined


class LLMBackend(ABC):
    """Abstract base class for Language Model backends."""

    @abstractmethod
    def generate_text(self, prompt: str) -> List[Tuple[str, str]]:
        """
        Generate text based on a given prompt.

        Args:
            prompt (str): The input prompt for text generation.

        Returns:
            List[Tuple[str, str]]: A list of tuples containing speaker and text.
        """
        pass


class TTSBackend(ABC):
    """Abstract base class for Text-to-Speech backends."""

    @abstractmethod
    def text_to_speech(self, text: str, voice: str) -> Union[Path, Coroutine[Any, Any, Path]]:
        """
        Convert text to speech.

        Args:
            text (str): The text to convert to speech.
            voice (str): The voice to use for speech synthesis.

        Returns:
            Union[Path, Coroutine[Any, Any, Path]]: Path to the generated audio file or a coroutine that returns the path.
        """
        pass

    @property
    def is_async(self) -> bool:
        """Check if the text_to_speech method is asynchronous."""
        return asyncio.iscoroutinefunction(self.text_to_speech)

    @property
    def has_sync_and_async(self) -> bool:
        """Check if the backend has both synchronous and asynchronous TTS methods."""
        return (hasattr(self, "text_to_speech_sync") and
                hasattr(self, "async_text_to_speech") and
                asyncio.iscoroutinefunction(self.async_text_to_speech))


class TranscriptSegment:
    """Represents a segment of the podcast transcript."""

    def __init__(self, text: str, speaker: str, tts_args: Optional[Dict[str, Any]] = None):
        self.text = text
        self.speaker = speaker
        self.tts_args = tts_args or {}


class Transcript:
    """Represents the full transcript of a podcast."""

    def __init__(self, segments: List[TranscriptSegment], metadata: Dict[str, Any]):
        self.segments = segments
        self.metadata = metadata

    def save(self, filepath: str, format: str = "plaintext"):
        """Save the transcript to a file."""
        with open(filepath, 'w') as f:
            f.write(str(self))

    def __str__(self) -> str:
        """Convert the transcript to a string representation."""
        lines = []
        for segment in self.segments:
            lines.append(f"{segment.speaker}: {segment.text}")

        metadata_str = "\n".join([f"{key}: {value}" for key, value in self.metadata.items()])

        return f"Metadata:\n{metadata_str}\n\nTranscript:\n" + "\n".join(lines)


class AudioSegment:
    """Represents an audio segment of the podcast."""

    def __init__(self, filepath: Path, length_ms: int, transcript_segment: Optional[TranscriptSegment] = None):
        self.filepath = filepath
        self.length_ms = length_ms
        self.transcript_segment = transcript_segment
        self._audio: Optional[PydubAudioSegment] = None

    @property
    def audio(self) -> PydubAudioSegment:
        """Lazy-load the audio segment."""
        if self._audio is None:
            self._audio = PydubAudioSegment.from_file(self.filepath)
            if len(self._audio) != self.length_ms:
                raise ValueError(
                    f"Audio file length ({len(self._audio)}ms) does not match specified length ({self.length_ms}ms)")
        return self._audio


def podcast_stage(func):
    """Decorator to manage podcast stage transitions."""

    @wraps(func)
    def wrapper(self, *args, **kwargs):
        current_method = self._next_stage_methods[self.state]
        if current_method != func and not self._reworking:
            print(f"Cannot execute {func.__name__} in current state {self.state.name}. Skipping.")
            return

        try:
            result = func(self, *args, **kwargs)
            next_state = next((state for state, method in self._next_stage_methods.items() if method == func), None)
            self.state = next_state if next_state else self.state
            return result
        except Exception as e:
            print(f"Error in {func.__name__}: {str(e)}")
            raise

    return wrapper


class Podcast:
    """Main class for podcast creation and management."""

    def __init__(self, content: str, llm_backend: LLMBackend, tts_backend: TTSBackend, default_tts_n_jobs: int = 1):
        """
        Initialize a new Podcast instance.

        Args:
            content (str): The raw content to be processed into a podcast.
            llm_backend (LLMBackend): The language model backend for generating the transcript.
            tts_backend (TTSBackend): The text-to-speech backend for converting text to audio.
            default_tts_n_jobs (int, optional): The default number of concurrent jobs for TTS processing.
                Defaults to 1.

        Raises:
            logging.warning: If using a synchronous TTS backend with multiple jobs.
        """
        self.content = content
        self.llm_backend = llm_backend
        self.tts_backend = tts_backend
        self.default_tts_n_jobs = default_tts_n_jobs
        self.state = PodcastState.INITIALIZED
        self._reworking = False

        # Initialize attributes with null values
        self.transcript = None
        self.audio_segments = []
        self.audio = None

        self.tts_has_async = self.tts_backend.has_sync_and_async
        if default_tts_n_jobs > 1 and not self.tts_backend.is_async:
            raise logging.warning(
                "Synchronous TTS backend with default_tts_n_jobs > 1. Threads will be used. Consider using an asynchronous TTS backend for better performance.")

        # Define the sequence of methods to be called for each stage
        self._next_stage_methods: Dict[PodcastState, Callable[[], None]] = {
            PodcastState.INITIALIZED: self.build_transcript,
            PodcastState.TRANSCRIPT_BUILT: self.build_audio_segments,
            PodcastState.AUDIO_SEGMENTS_BUILT: self.stitch_audio_segments,
        }

    def reset_to_state(self, state: PodcastState):
        """Reset the podcast to a specific state."""
        self.state = state
        self.transcript = None if state.value < PodcastState.TRANSCRIPT_BUILT.value else self.transcript
        self.audio_segments = [] if state.value < PodcastState.AUDIO_SEGMENTS_BUILT.value else self.audio_segments
        self.audio = None if state.value < PodcastState.STITCHED.value else self.audio

    @contextmanager
    def rework(self, target_state: PodcastState, auto_finalize: bool = True):
        """Context manager for reworking the podcast from a specific state."""
        original_state = self.state
        self._reworking = True

        if target_state.value < self.state.value:
            print(f"Rewinding from {self.state.name} to {target_state.name}")
            self.reset_to_state(target_state)

        try:
            yield
        finally:
            self._reworking = False
            if self.state.value < original_state.value:
                print(
                    f"Warning: Podcast is now in an earlier state ({self.state.name}) than before reworking ({original_state.name}). You may want to call finalize() to rebuild.")
                if auto_finalize:
                    self.finalize()

    @podcast_stage
    def build_transcript(self) -> None:
        """Build the podcast transcript using the LLM backend."""
        generated_segments = self.llm_backend.generate_text(self.content)

        segments = [TranscriptSegment(text, speaker) for speaker, text in generated_segments]

        self.transcript = Transcript(segments, {"source": "Generated content"})

    async def _async_text_to_speech(self, segment: TranscriptSegment) -> AudioSegment:
        """Asynchronously convert a transcript segment to speech."""
        audio_file = await self.tts_backend.text_to_speech(segment.text, segment.speaker)
        audio_segment = PydubAudioSegment.from_file(audio_file)
        return AudioSegment(audio_file, len(audio_segment), segment)

    def _sync_text_to_speech(self, segment: TranscriptSegment) -> AudioSegment:
        """Synchronously convert a transcript segment to speech."""
        audio_file = self.tts_backend.text_to_speech(segment.text, segment.speaker)
        audio_segment = PydubAudioSegment.from_file(audio_file)
        return AudioSegment(audio_file, len(audio_segment), segment)

    @podcast_stage
    def build_audio_segments(self, n_jobs: Optional[int] = None) -> None:
        """Build audio segments from the transcript."""
        n_jobs = n_jobs or self.default_tts_n_jobs

        if self.tts_has_async:
            async def process_segments():
                tasks = []
                sem = asyncio.Semaphore(n_jobs)

                async def bounded_tts(segment):
                    async with sem:
                        return await self._async_text_to_speech(segment)

                for segment in self.transcript.segments:
                    tasks.append(asyncio.create_task(bounded_tts(segment)))

                self.audio_segments = await asyncio.gather(*tasks)

            asyncio.run(process_segments())
        else:
            with ThreadPoolExecutor(max_workers=n_jobs) as executor:
                future_to_segment = {executor.submit(self._sync_text_to_speech, segment): segment
                                     for segment in self.transcript.segments}
                self.audio_segments = []
                for future in as_completed(future_to_segment):
                    self.audio_segments.append(future.result())

        # Sort audio segments based on their order in the transcript
        self.audio_segments.sort(key=lambda x: self.transcript.segments.index(x.transcript_segment))

    @podcast_stage
    def stitch_audio_segments(self) -> None:
        """Stitch all audio segments together to form the final podcast audio."""
        self.audio = sum([segment.audio for segment in self.audio_segments])

    def _build_next_stage(self) -> bool:
        """Build the next stage of the podcast."""
        if self.state == PodcastState.STITCHED:
            return False

        next_method = self._next_stage_methods[self.state]
        next_method()
        return True

    def finalize(self) -> None:
        """Finalize the podcast by building all remaining stages."""
        while self._build_next_stage():
            pass

    def save(self, filepath: str) -> None:
        """Save the finalized podcast audio to a file."""
        if self.state != PodcastState.STITCHED:
            raise ValueError("Podcast can only be saved after audio is stitched")

        if self.audio:
            self.audio.export(filepath, format="mp3")
        else:
            raise ValueError("No stitched audio to save")

    def save_transcript(self, filepath: str, format: str = "plaintext") -> None:
        """Save the podcast transcript to a file."""
        if self.state < PodcastState.TRANSCRIPT_BUILT:
            raise ValueError("Transcript can only be saved after it is built")

        if self.transcript:
            self.transcript.save(filepath, format)
        else:
            raise ValueError("No transcript to save")


# Usage example: Step-by-step podcast creation
if __name__ == "__main__":
    from tempfile import NamedTemporaryFile


    class DummyLLMBackend(LLMBackend):
        def generate_text(self, prompt: str) -> List[Tuple[str, str]]:
            return [("Host", "Welcome to our podcast!"), ("Guest", "Thanks for having me!")]


    class DummyTTSBackend(TTSBackend):
        def text_to_speech(self, text: str, voice: str) -> Path:
            with NamedTemporaryFile(suffix=".mp3", delete=False) as temp_file:
                PydubAudioSegment.silent(duration=1000).export(temp_file.name, format="mp3")
            return Path(temp_file.name)


    # Initialize the podcast
    podcast = Podcast(
        content="""
        This is a sample content for our podcast.
        It includes information from multiple sources that have already been parsed.
        """,
        llm_backend=DummyLLMBackend(),
        tts_backend=DummyTTSBackend(),
    )
    print(f"Initial state: {podcast.state}")

    # Step 1: Build transcript
    podcast.build_transcript()
    print(f"After building transcript: {podcast.state}")
    print(f"Transcript: {podcast.transcript}")

    # Step 2: Build audio segments
    podcast.build_audio_segments()
    print(f"After building audio segments: {podcast.state}")
    print(f"Number of audio segments: {len(podcast.audio_segments)}")

    # Step 3: Stitch audio segments
    podcast.stitch_audio_segments()
    print(f"After stitching audio: {podcast.state}")

    # Rework example: modify the transcript and rebuild (auto_finalize is True by default)
    with podcast.rework(PodcastState.TRANSCRIPT_BUILT):
        print(f"Inside rework context, state: {podcast.state}")
        podcast.transcript.segments.append(TranscriptSegment("This is a new segment", "Host"))
        print("Added new segment to transcript")

        # Rebuild audio segments and stitch
        podcast.build_audio_segments()

    print(f"After rework: {podcast.state}")

    # Add a new audio segment (auto_finalize is True by default)
    with NamedTemporaryFile(suffix=".mp3", delete=False) as temp_file:
        PydubAudioSegment.silent(duration=500).export(temp_file.name, format="mp3")

    with podcast.rework(PodcastState.AUDIO_SEGMENTS_BUILT):
        new_segment = AudioSegment(Path(temp_file.name), 500, TranscriptSegment("New audio segment", "Host"))
        podcast.audio_segments.insert(0, new_segment)

    # Save the final podcast
    podcast.save("./final.mp3")
    podcast.save_transcript("./final.txt", format="plaintext")
    print("Saved podcast and transcript")