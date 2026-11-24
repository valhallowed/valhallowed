"""Text vhd shows the server manager that came from a child or a container: their words, not vhd's."""


def printable(text: str) -> str:
    """The text with each character a terminal would act on, such as an escape, as ?."""
    return "".join(c if c.isprintable() else "?" for c in text)
