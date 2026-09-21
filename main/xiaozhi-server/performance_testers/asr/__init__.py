def create_benchmark(provider_name, provider_config):
    if provider_config.get("type") == "gemini":
        from .gemini import GeminiASRBenchmark

        return GeminiASRBenchmark(provider_name, provider_config)

    from .batch import BatchASRBenchmark

    return BatchASRBenchmark(provider_name, provider_config)
