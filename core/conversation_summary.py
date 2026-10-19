"""Render sourced summary entries atomically, including citation overhead."""


def render_summary(items,max_chars=None):
    lines=[]
    used=0
    for item in items:
        line=item['text']+' '+''.join(f"[{ref['message_id']}]" for ref in item['sources'])
        size=len(line)+(1 if lines else 0)
        if max_chars is not None and used+size>max_chars:
            break
        lines.append(line)
        used+=size
    return '\n'.join(lines)
