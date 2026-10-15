"""Natural ordering for component references and numbered terminals."""
import re


def natural_key(text):
    return tuple((1, int(part)) if part.isdigit() else (0, part.casefold())
                 for part in re.split(r'(\d+)', str(text)))
