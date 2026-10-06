"""Model-key placeholder check without audio or server runtime imports."""


def check_model_key(modelType, modelKey):
    if "\u4f60" in modelKey:
        return f"Configuration error: {modelType} API key is not set"
    return None
