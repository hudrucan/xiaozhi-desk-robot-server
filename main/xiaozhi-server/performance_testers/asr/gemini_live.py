import asyncio
import contextlib
import time

from google import genai
from google.genai import types

from .base import ASRBenchmarkBase


class GeminiLiveASRBenchmark(ASRBenchmarkBase):
    def live_config(self):
        language = self.provider_config.get("language", "auto")
        language_codes = [] if language == "auto" else [language]
        mode = str(self.provider_config.get("mode", "VERBATIM")).upper()
        return types.LiveConnectConfig(
            response_modalities=["TEXT"],
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    disabled=True
                )
            ),
            input_audio_transcription=types.AudioTranscriptionConfig(
                language_codes=language_codes,
                mode=mode,
            ),
        )

    @staticmethod
    async def receive_final(session, turn_started_at, activity_ending):
        first_interim = None
        async for response in session.receive():
            server_content = response.server_content
            if not server_content:
                continue
            interim = server_content.interim_input_transcription
            if interim and interim.text and first_interim is None:
                first_interim = time.perf_counter() - turn_started_at
            final = server_content.input_transcription
            if final and final.text and activity_ending.is_set():
                return final.text.strip(), first_interim, time.perf_counter()
        raise RuntimeError("Gemini Live session ended before returning a transcript")

    async def run(self):
        chunk_ms = self.get_setting("PERF_AUDIO_CHUNK_MS", 120)
        chunk_bytes = chunk_ms * 32
        client = genai.Client(api_key=self.provider_config.get("api_key"))
        model_name = self.provider_config.get(
            "model_name", "gemini-3.5-transcribe-live"
        )
        first_interim_times = []
        finalization_times = []
        total_times = []

        self.print_header(model_name)
        connect_started_at = time.perf_counter()
        try:
            async with client.aio.live.connect(
                model=model_name,
                config=self.live_config(),
            ) as session:
                connect_time = time.perf_counter() - connect_started_at
                print(f"Live session connected in {connect_time:.3f}s")

                for run_number in range(1, self.runs + 1):
                    turn_started_at = time.perf_counter()
                    activity_ending = asyncio.Event()
                    receive_task = asyncio.create_task(
                        self.receive_final(
                            session, turn_started_at, activity_ending
                        )
                    )
                    try:
                        await session.send_realtime_input(
                            activity_start=types.ActivityStart()
                        )
                        for offset in range(0, len(self.pcm_data), chunk_bytes):
                            chunk = self.pcm_data[offset : offset + chunk_bytes]
                            await session.send_realtime_input(
                                audio=types.Blob(
                                    data=chunk,
                                    mime_type="audio/pcm;rate=16000",
                                )
                            )
                            await asyncio.sleep(len(chunk) / 32000)

                        speech_ended_at = time.perf_counter()
                        activity_ending.set()
                        await session.send_realtime_input(
                            activity_end=types.ActivityEnd()
                        )
                        transcript, first_interim, final_received_at = (
                            await asyncio.wait_for(
                                receive_task,
                                timeout=self.timeout,
                            )
                        )
                        finalization_time = final_received_at - speech_ended_at
                        total_time = final_received_at - turn_started_at
                        finalization_times.append(finalization_time)
                        total_times.append(total_time)
                        if first_interim is not None:
                            first_interim_times.append(first_interim)
                        print(
                            f"Run {run_number}/{self.runs}: final after speech "
                            f"end {finalization_time:.3f}s, total "
                            f"{total_time:.3f}s - {transcript[:100]}"
                        )
                    except Exception as error:
                        receive_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await receive_task
                        print(
                            f"Run {run_number}/{self.runs}: failed - "
                            f"{type(error).__name__}: {error}"
                        )
                        break
        finally:
            await client.aio.aclose()

        print("\nGemini Live ASR benchmark summary")
        print(f"Success rate: {len(finalization_times)}/{self.runs}")
        if first_interim_times:
            print(self.format_stats("First interim", first_interim_times))
        if finalization_times:
            print(self.format_stats("Final after speech end", finalization_times))
            print(self.format_stats("Total turn", total_times))
