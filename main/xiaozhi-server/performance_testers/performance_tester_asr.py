import asyncio
import logging

from config.settings import load_config
from performance_testers.asr import create_benchmark


logging.basicConfig(level=logging.WARNING)

description = "Benchmark the selected ASR provider"


async def main():
    config = await load_config()
    provider_name = config.get("selected_module", {}).get("ASR")
    provider_config = config.get("ASR", {}).get(provider_name)
    if not provider_name or provider_config is None:
        raise ValueError("The selected ASR provider is not configured")

    await create_benchmark(provider_name, provider_config).run()


if __name__ == "__main__":
    asyncio.run(main())
