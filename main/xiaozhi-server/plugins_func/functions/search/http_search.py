"""Existing HTTP search adapters without runtime configuration or logging."""
import httpx

async def _search_metaso(api_key: str, query: str, max_results: int) -> str:
    """Call the Metaso search API."""
    url = 'https://metaso.cn/api/v1/search'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {'q': query, 'size': max_results, 'stream': False, 'scope': 'webpage', 'includeSummary': True, 'includeRawContent': False, 'conciseSnippet': False}
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    data = response.json()
    webpages = data.get('webpages', [])
    if not webpages:
        return 'No relevant search results were found.'
    lines = ['【联网搜索结果】']
    for i, item in enumerate(webpages, 1):
        title = item.get('title', '无标题')
        snippet = item.get('summary', '')
        date = item.get('date', '')
        lines.append(f'{i}. 标题：{title}')
        if date:
            lines.append(f'   日期：{date}')
        if snippet:
            lines.append(f'   摘要：{snippet}')
    return '\n'.join(lines)

async def _search_tavily(api_key: str, query: str, max_results: int, search_depth: str='advanced', include_answer: str | bool='advanced', country: str='', language: str='') -> str:
    """Call the Tavily search API."""
    url = 'https://api.tavily.com/search'
    headers = {'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'}
    payload = {'query': query, 'max_results': max_results, 'search_depth': search_depth, 'include_answer': include_answer}
    if country:
        payload['country'] = country
    if language:
        payload['language'] = language
    async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
        response = await client.post(url, json=payload, headers=headers)
    response.raise_for_status()
    data = response.json()
    results = data.get('results', [])
    if not results:
        return 'No relevant search results were found.'
    answer = data.get('answer', '')
    lines = ['【Web search results】']
    if answer:
        lines.append(f'Summary: {answer}')
    for i, item in enumerate(results, 1):
        title = item.get('title', 'Untitled')
        url = item.get('url', '')
        summary = item.get('content', '')
        lines.append(f'{i}. {title}')
        if url:
            lines.append(f'   URL: {url}')
        if summary:
            lines.append(f'   Snippet: {summary}')
    return '\n'.join(lines)
