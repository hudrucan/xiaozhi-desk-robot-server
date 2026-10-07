"""Shared tool declarations without registration or configuration side effects."""

GET_CURRENT_DATETIME_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_current_datetime",
        "description": (
            "Get the server's authoritative current local date, weekday, time, "
            "and UTC offset. Call this on every request for the current date, "
            "day of the week, or time, even if an earlier turn contains a "
            "previous result."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

GET_WEATHER_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Get current conditions and a multi-day forecast for a location. "
            "Call this on every request for current weather or a forecast, even "
            "if context or an earlier turn contains a previous result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Optional city or location name.",
                },
                "lang": {
                    "type": "string",
                    "description": "Optional ISO 639-1 language code for place names.",
                },
            },
            "required": [],
        },
    },
}

GET_AIR_QUALITY_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "get_air_quality",
        "description": (
            "Get current air quality, particle pollution, UV index, and a short "
            "forecast summary for a location. Call this on every request for "
            "current air quality, even if an earlier turn contains a previous "
            "result."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": "Optional city or location name.",
                },
                "lang": {
                    "type": "string",
                    "description": "Optional ISO 639-1 language code for place names.",
                },
                "forecast_hours": {
                    "type": "integer",
                    "description": "Optional forecast window from 1 to 168 hours.",
                    "minimum": 1,
                    "maximum": 168,
                },
            },
            "required": [],
        },
    },
}

WEB_SEARCH_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": 'Search the web when the user explicitly needs current online information.',
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query or question.",
                }
            },
            "required": ["query"],
        },
    },
}

handle_exit_intent_function_desc = {
    "type": "function",
    "function": {
        "name": "handle_exit_intent",
        "description": (
            "End the current conversation and close the connection after the "
            "farewell. Always call this tool instead of replying normally when "
            "the user explicitly says goodbye or farewell, asks to end or close "
            "the conversation, disconnect, or tells the assistant or robot to "
            "go to sleep. Do not call it when ending the conversation is only "
            "mentioned rather than requested."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}


MANAGE_MEMORY_FUNCTION_DESC = {
    "type": "function",
    "function": {
        "name": "manage_memory",
        "description": (
            "Manage durable local memory only when the user explicitly asks to "
            "remember or forget information, asks what is saved, or asks a "
            "question that depends on a previously saved personal fact. Use "
            "recall before answering questions about saved facts. Never save "
            "ordinary conversation automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["remember", "recall", "forget", "list"],
                },
                "content": {
                    "type": "string",
                    "description": (
                        "A concise fact to save, or a short search phrase for "
                        "recall or deletion. Omit only for list."
                    ),
                },
                "type": {
                    "type": "string",
                    "enum": [
                        "fact", "preference", "decision", "project_state",
                        "hardware", "todo", "session",
                    ],
                    "description": "Memory category. Used only when remembering.",
                },
                "project": {
                    "type": "string",
                    "description": "Optional project scope. Omit for global memory.",
                },
                "entities": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Exact names, components, people, or devices.",
                },
                "tags": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "importance": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                },
                "pinned": {"type": "boolean"},
                "active": {"type": "boolean"},
                "supersedes": {
                    "type": "string",
                    "description": (
                        "ID of an older memory replaced by this new memory. "
                        "Use only when the replacement is explicit."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}
