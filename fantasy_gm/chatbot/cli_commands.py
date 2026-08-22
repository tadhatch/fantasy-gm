from __future__ import annotations

import json

import typer
from rich.console import Console

from fantasy_gm.config import get_settings
from fantasy_gm.espn.client import ESPNClient

from .espn_transport import EspnChatTransport
from .local import run_local_chatbot
from .models import ChatMessage, ChatReaction, ChatReactionEvent
from .runtime import run_chatbot
from .settings import ChatbotSettings


def build_chatbot_typer() -> typer.Typer:
    app = typer.Typer(no_args_is_help=True, help="ESPN league chatbot tools")
    console = Console()

    @app.command("run")
    def chatbot_run(
        league_id: int | None = typer.Option(
            None,
            "--league-id",
            help="Override configured ESPN league ID",
        ),
        topic_id: str | None = typer.Option(
            None,
            "--topic-id",
            help=(
                "Force ESPN chat topic ID. Overrides "
                "FANTASY_GM_CHAT_TOPIC_ID and auto-discovery."
            ),
        ),
        mode: str | None = typer.Option(
            None,
            "--mode",
            help="shadow, respond, or full",
        ),
    ) -> None:
        run_chatbot(
            league_id=league_id,
            topic_id=topic_id,
            mode=mode,
        )

    @app.command("watch")
    def chatbot_watch(
        league_id: int | None = typer.Option(None, "--league-id"),
        topic_id: str | None = typer.Option(None, "--topic-id"),
        include_self: bool = typer.Option(False, "--include-self"),
    ) -> None:
        settings = get_settings()
        chat = ChatbotSettings.from_env(topic_id=topic_id)
        client = ESPNClient(settings, league_id=league_id)
        transport = EspnChatTransport(
            client,
            topic_id=chat.topic_id,
            poll_seconds=chat.poll_seconds,
        )
        transport.connect()
        console.print(
            f"[green]watching chat[/green] "
            f"league={client.league_id} topic={transport.topic_id}"
        )
        try:
            while True:
                event = transport.receive(timeout=10)
                if event is None:
                    continue
                if isinstance(event, ChatMessage):
                    if event.is_self and not include_self:
                        continue
                    console.print(
                        f"[bold]{event.author}[/bold]: {event.text} "
                        f"[dim]({event.id})[/dim]"
                    )
                elif isinstance(event, ChatReactionEvent):
                    if event.is_self and not include_self:
                        continue
                    console.print(
                        f"[dim]reaction "
                        f"{'+' if event.added else '-'}{event.reaction.label} "
                        f"message={event.message_id} user={event.user_id}[/dim]"
                    )
        finally:
            transport.close()

    @app.command("send")
    def chatbot_send(
        text: str = typer.Argument(...),
        league_id: int | None = typer.Option(None, "--league-id"),
        topic_id: str | None = typer.Option(None, "--topic-id"),
    ) -> None:
        settings = get_settings()
        chat = ChatbotSettings.from_env(topic_id=topic_id)
        client = ESPNClient(settings, league_id=league_id)
        transport = EspnChatTransport(
            client,
            topic_id=chat.topic_id,
            poll_seconds=chat.poll_seconds,
        )
        transport.connect()
        transport.send(text)
        console.print(
            f"[green]sent[/green] topic={transport.topic_id}: {text}"
        )

    @app.command("react")
    def chatbot_react(
        message_id: str = typer.Argument(...),
        reaction: str = typer.Argument(
            ...,
            help="funny|heart|shock|trophy|anger|fire|dislike|like",
        ),
        league_id: int | None = typer.Option(None, "--league-id"),
        topic_id: str | None = typer.Option(None, "--topic-id"),
    ) -> None:
        parsed = ChatReaction.from_name(reaction)
        if parsed is None:
            raise typer.BadParameter(f"unknown reaction: {reaction}")

        settings = get_settings()
        chat = ChatbotSettings.from_env(topic_id=topic_id)
        client = ESPNClient(settings, league_id=league_id)
        transport = EspnChatTransport(
            client,
            topic_id=chat.topic_id,
            poll_seconds=chat.poll_seconds,
        )
        transport.connect()
        transport.react(message_id, parsed)
        console.print(
            f"[green]reacted[/green] {parsed.label} "
            f"message={message_id} topic={transport.topic_id}"
        )

    @app.command("topic")
    def chatbot_topic(
        league_id: int | None = typer.Option(None, "--league-id"),
        topic_id: str | None = typer.Option(None, "--topic-id"),
    ) -> None:
        settings = get_settings()
        chat = ChatbotSettings.from_env(topic_id=topic_id)
        client = ESPNClient(settings, league_id=league_id)
        transport = EspnChatTransport(
            client,
            topic_id=chat.topic_id,
            poll_seconds=chat.poll_seconds,
        )
        transport.connect()
        console.print(str(transport.topic_id))

    @app.command("test")
    def chatbot_test(
        mode: str = typer.Option("respond", "--mode"),
    ) -> None:
        run_local_chatbot(mode=mode)

    return app
