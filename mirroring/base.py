"""Base management command with consistent verbosity-aware output."""

from __future__ import annotations

from django.core.management.base import BaseCommand


class BaseMirroringCommand(BaseCommand):
    """Slim base for mirroring management commands (no tqdm/parallel helpers)."""

    verbosity: int = 1

    def execute(self, *args, **options):
        """Execute the command, automatically setting verbosity from options."""
        self.verbosity = options.get("verbosity", 1)
        return super().execute(*args, **options)

    def render_h1(self, text: str, pad_bottom: bool = True, verbosity: int = 1) -> None:
        """Display a heading 1 with consistent styling."""
        if self.verbosity < verbosity:
            return
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING("=" * max(50, len(text))))
        self.stdout.write(self.style.MIGRATE_HEADING(text))
        self.stdout.write(self.style.MIGRATE_HEADING("=" * max(50, len(text))))
        if pad_bottom:
            self.stdout.write("")

    def render_h2(self, text: str, pad_bottom: bool = True, verbosity: int = 1) -> None:
        """Display a heading 2 with consistent styling."""
        if self.verbosity < verbosity:
            return
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("-" * max(50, len(text))))
        self.stdout.write(self.style.WARNING(text))
        self.stdout.write(self.style.WARNING("-" * max(50, len(text))))
        if pad_bottom:
            self.stdout.write("")

    def _conditional_write(
        self,
        text: str,
        style=None,
        stream=None,
        verbosity: int = 1,
        *,
        ending: str = "\n",
        flush: bool = False,
    ) -> None:
        if self.verbosity >= verbosity:
            styled_text = style(text) if style else text
            output_stream = stream or self.stdout
            output_stream.write(styled_text, ending=ending)
            if flush:
                output_stream.flush()

    def info(
        self,
        text: str,
        verbosity: int = 1,
        *,
        ending: str = "\n",
        flush: bool = False,
    ) -> None:
        """Write the supplied text to stdout with default styling."""
        self._conditional_write(text, verbosity=verbosity, ending=ending, flush=flush)

    def success(self, text: str, verbosity: int = 1) -> None:
        """Write the supplied text to stdout with SUCCESS styling."""
        self._conditional_write(text, style=self.style.SUCCESS, verbosity=verbosity)

    def warning(self, text: str, verbosity: int = 1) -> None:
        """Write the supplied text to stdout with WARNING styling."""
        self._conditional_write(text, style=self.style.WARNING, verbosity=verbosity)

    def error(self, text: str, verbosity: int = 1) -> None:
        """Write the supplied text to stderr with ERROR styling."""
        self._conditional_write(text, style=self.style.ERROR, stream=self.stderr, verbosity=verbosity)
