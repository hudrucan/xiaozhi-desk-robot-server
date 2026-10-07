"""Small provider-free session controls exported from shared desired config."""
import copy

DEFAULTS = {'enable_greeting':True, 'wakeup_greeting':'Hello',
    'wakeup_words':['Hello Xiaozhi', 'Hey Xiaozhi'],
    'enable_wakeup_words_response_cache':True, 'exit_commands':['exit', 'quit'],
    'exit_farewell':'Goodbye, see you next time!'}


def validate(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULTS):
        raise ValueError('Invalid session controls')
    for key in ('enable_greeting', 'enable_wakeup_words_response_cache'):
        if type(value[key]) is not bool:
            raise ValueError('Invalid session boolean')
    for key in ('wakeup_greeting', 'exit_farewell'):
        if not isinstance(value[key], str) or not value[key].strip() or len(value[key].encode()) > 2048:
            raise ValueError('Invalid configured session response')
    for key in ('wakeup_words', 'exit_commands'):
        words = value[key]
        if (not isinstance(words, list) or len(words) > 64
                or any(not isinstance(word, str) or not word.strip() or len(word.encode()) > 512 for word in words)):
            raise ValueError('Invalid configured session commands')
    return value


def export_config(config):
    value = {key:copy.deepcopy(config.get(key, default)) for key,default in DEFAULTS.items()}
    if isinstance(value['wakeup_words'], str):
        value['wakeup_words'] = [value['wakeup_words']]
    return validate(value)
