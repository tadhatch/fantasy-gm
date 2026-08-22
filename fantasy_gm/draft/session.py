from __future__ import annotations

import queue
import random
import threading
import time
from dataclasses import dataclass

import websocket

from fantasy_gm.espn.client import ESPNClient

from .protocol import DraftEvent, Pong, Selected, Selecting, parse_message


class DraftSessionError(RuntimeError):
    pass


class DraftSelectionTimeout(DraftSessionError):
    pass


@dataclass(slots=True)
class DraftSessionConfig:
    league_id: int
    team_id: int
    member_id: str
    security_token: str
    game_id: int = 1
    client_name: str = "KONA"

@dataclass(slots=True)
class _ConnectionError:
    error: BaseException

class DraftSession:
    """
    Live ESPN draft-room WebSocket session.

    Observed ESPN browser flow:
      GET .../teams/{team_id}/draftSecurity -> short numeric token
      wss://fantasydraft.espn.com/game-1/league-{league_id}/JOIN?...token...
      client heartbeat: PING PING%20<epoch_ms>
      selection: SELECT <espn_player_id>
      acknowledgement: SELECTED <team_id> <player_id> ...
    """

    def __init__(
        self,
        client: ESPNClient,
        *,
        league_id: int | None = None,
        team_id: int | None = None,
        heartbeat_seconds: float = 14.0,
    ):
        self.client = client
        self.league_id = league_id or client.league_id
        self.team_id = team_id or client.settings.espn_team_id
        self.member_id = client.settings.espn_swid
        self.heartbeat_seconds = heartbeat_seconds

        self.ws: websocket.WebSocket | None = None
        self._events: queue.Queue[
            DraftEvent | _ConnectionError
        ] = queue.Queue()

        self._stop = threading.Event()

        self._reader_thread: threading.Thread | None = None
        self._heartbeat_thread: threading.Thread | None = None

        self._ack_condition = threading.Condition()

        self._selected_acks: dict[
            tuple[int, int],
            Selected,
        ] = {}

        self._connection_error: BaseException | None = None

    def bootstrap(self) -> DraftSessionConfig:
        security = self.client.get_draft_security(
            team_id=self.team_id,
            league_id=self.league_id,
        )

        token = str(security).strip()
        if not token:
            raise DraftSessionError("ESPN returned an empty draftSecurity token")

        return DraftSessionConfig(
            league_id=self.league_id,
            team_id=self.team_id,
            member_id=self.member_id,
            security_token=token,
        )

    def build_url(self, config: DraftSessionConfig) -> str:
        join_token = (
            f"1:{config.league_id}:{config.team_id}:"
            f"{config.member_id}:{config.security_token}"
        )

        return (
            f"wss://fantasydraft.espn.com/"
            f"game-{config.game_id}/league-{config.league_id}/JOIN?"
            f"1={config.game_id}"
            f"&2={config.league_id}"
            f"&3={config.team_id}"
            f"&4={config.member_id}"
            f"&5={join_token}"
            f"&6=false"
            f"&7=false"
            f"&8={config.client_name}"
            f"&nocache={random.randint(100000, 999999)}"
        )

    def connect(self) -> None:
        config = self.bootstrap()
        print(
            "draft bootstrap:",
            f"league={config.league_id}",
            f"team={config.team_id}",
            f"member={config.member_id}",
            f"token_len={len(config.security_token)}",
        )
        url = self.build_url(config)

        self.ws = websocket.create_connection(
            url,
            origin="https://fantasy.espn.com",
            timeout=10,
            enable_multithread=True,
            header=[
                (
                    "User-Agent: Mozilla/5.0 "
                    "(Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/145.0.0.0 Safari/537.36"
                ),
                "Cache-Control: no-cache",
                "Pragma: no-cache",
            ],
        )

        self.ws.settimeout(1.0)

        self._stop.clear()
        self._connection_error = None

        self._reader_thread = threading.Thread(
            target=self._reader_loop,
            name="espn-draft-reader",
            daemon=True,
        )
        self._reader_thread.start()

        self._heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="espn-draft-heartbeat",
            daemon=True,
        )
        self._heartbeat_thread.start()


    def close(self) -> None:
        self._stop.set()
        if self.ws is not None:
            try:
                self.ws.close()
            finally:
                self.ws = None

    def send_select(self, player_id: int) -> None:
        if self.ws is None:
            raise DraftSessionError("Draft session is not connected")

        self.ws.send(f"SELECT {int(player_id)}\n")

    def select_and_wait(
        self,
        player_id: int,
        *,
        timeout: float = 5.0,
    ) -> Selected:
        player_id = int(player_id)
        key = (self.team_id, player_id)

        with self._ack_condition:
            self._selected_acks.pop(key, None)

        self.send_select(player_id)

        deadline = time.monotonic() + timeout

        with self._ack_condition:
            while True:
                ack = self._selected_acks.get(key)

                if ack is not None:
                    return ack

                if self._connection_error is not None:
                    raise DraftSessionError(
                        "Draft WebSocket failed while waiting "
                        f"for ESPN acknowledgement: "
                        f"{self._connection_error}"
                    )

                remaining = deadline - time.monotonic()

                if remaining <= 0:
                    raise DraftSelectionTimeout(
                        f"No matching ESPN SELECTED acknowledgement "
                        f"for player {player_id} within {timeout:.1f}s"
                    )

                self._ack_condition.wait(timeout=remaining)

    def next_event(
        self,
        timeout: float | None = None,
    ) -> DraftEvent:
        try:
            item = self._events.get(timeout=timeout)
        except queue.Empty:
            raise websocket.WebSocketTimeoutException(
                "No draft event before timeout"
            )

        if isinstance(item, _ConnectionError):
            raise DraftSessionError(
                f"ESPN draft WebSocket reader failed: {item.error}"
            )

        return item

    def events(self):
        while not self._stop.is_set():
            try:
                yield self.next_event(timeout=30.0)
            except websocket.WebSocketTimeoutException:
                continue

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            if self.ws is None:
                return

            epoch_ms = int(time.time() * 1000)
            payload = f"PING%20{epoch_ms}"

            try:
                self.ws.send(f"PING {payload}\n")
            except Exception:
                return

    def __enter__(self) -> "DraftSession":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _reader_loop(self) -> None:
        assert self.ws is not None

        while not self._stop.is_set():
            try:
                message = self.ws.recv()

            except websocket.WebSocketTimeoutException:
                continue

            except Exception as exc:
                if self._stop.is_set():
                    return

                self._connection_error = exc
                self._events.put(_ConnectionError(exc))

                with self._ack_condition:
                    self._ack_condition.notify_all()

                return

            if message is None:
                exc = DraftSessionError(
                    "ESPN closed the draft WebSocket"
                )

                self._connection_error = exc
                self._events.put(_ConnectionError(exc))

                with self._ack_condition:
                    self._ack_condition.notify_all()

                return

            if isinstance(message, bytes):
                print(
                    f"[WS BINARY] len={len(message)} "
                    f"prefix={message[:120]!r}"
                )
                continue

            event = parse_message(message)

            if isinstance(event, Selected):
                key = (
                    event.team_id,
                    event.player_id,
                )

                with self._ack_condition:
                    self._selected_acks[key] = event
                    self._ack_condition.notify_all()

            self._events.put(event)