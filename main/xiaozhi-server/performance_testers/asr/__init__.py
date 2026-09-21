def create_benchmark(provider_name, provider_config):
    if provider_config.get("type") == "gemini_live":
        from .gemini_live import GeminiLiveASRBenchmark

        return GeminiLiveASRBenchmark(provider_name, provider_config)

    from .batch import BatchASRBenchmark

    return BatchASRBenchmark(provider_name, provider_config)
