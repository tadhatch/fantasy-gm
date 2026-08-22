from __future__ import annotations

import time

from dataclasses import dataclass

from rich.console import Console

from fantasy_gm.board.builder import build_board
from fantasy_gm.espn.client import ESPNClient
from fantasy_gm.espn.constants import defense_name
from fantasy_gm.defense.stats import load_defense_stats
from fantasy_gm.defense.builder import build_defense_board_players

from .init import (
    InitPick,
    decode_init_picks,
)

from .protocol import (
    AutoDraft,
    Init,
    Selected,
    Selecting,
)

from .session import (
    DraftSelectionTimeout,
    DraftSessionError,
    DraftSession,
)
from .strategy import RosterRules, RosterStrategy


@dataclass(slots=True)
class DraftRunnerConfig:
    auto_select: bool = False
    ack_timeout: float = 5.0
    board_pool_limit: int = 1000
    roster_size: int = 15


class DraftRunner:
    """
    Owns ONE ESPN draft WebSocket and uses that same connection for both
    receiving live state and submitting our picks.

    Important:
      FANTASY_GM_AUTO_SELECT / auto_select controls *our* selector.
      ESPN's timeout autodraft is a separate ESPN behavior.
    """

    def __init__(
        self,
        client: ESPNClient,
        *,
        team_id: int,
        favorite_team: str | None = None,
        fandom_weight: float = 1.5,
        config: DraftRunnerConfig | None = None,
    ):
        self.client = client
        self.team_id = team_id
        self.favorite_team = favorite_team
        self.fandom_weight = fandom_weight
        self.config = config or DraftRunnerConfig()

        self.last_overall_pick = 0
        self.pending_selection: int | None = None
        self.reconnect_count = 0

        self.console = Console()
        self.taken: set[int] = set()
        self.team_rosters: dict[int, list[int]] = {}
        self.player_names: dict[int, str] = {}
        self.board = []

        self.strategy = RosterStrategy(
            RosterRules(
                roster_size=15,
                league_size=14,
            )
        )

    def prepare(self) -> None:
        self.console.print(
            "[dim]Preparing minimal draft board before connecting...[/dim]"
        )
    
        data = self.client.get_player_pool(
            limit=self.config.board_pool_limit
        )
        pool = data.get("players", []) if isinstance(data, dict) else data
    
        self.board = build_board(
            pool,
            season=self.client.settings.espn_season,
            next_pick=None,
            history_season=None,
            favorite_team=self.favorite_team,
            fandom_weight=self.fandom_weight,
        )
    
        self.player_names = {
            row.espn_id: row.name
            for row in self.board
        }
    
        self.console.print(
            f"[green]Board ready:[/green] "
            f"{len(self.board)} draftable assets."
        )

#    def prepare(self) -> None:
#        """
#        Build the expensive quantitative/context board BEFORE opening the WebSocket.
#        This keeps our response on the 30/60-second clock fast.
#        """
#        self.console.print("[dim]Preparing draft board before connecting...[/dim]")
#
#        data = self.client.get_player_pool(limit=self.config.board_pool_limit)
#        pool = data.get("players", []) if isinstance(data, dict) else data
#
#        # For the live selection test we deliberately avoid recomputing a mock-specific
#        # Next% target. The existing Value/Pick scores, AI cache, usage, depth, fandom,
#        # etc. still apply. We will add exact roster/next-pick strategy next.
#        self.board = build_board(
#            pool,
#            season=self.client.settings.espn_season,
#            next_pick=None,
#            history_season=self.client.settings.espn_season - 1,
#            favorite_team=self.favorite_team,
#            fandom_weight=self.fandom_weight,
#        )
#
#        defense_stats = load_defense_stats(
#            base_season=self.client.settings.espn_season - 1,
#            schedule_season=self.client.settings.espn_season,
#            early_weeks=4,
#        )
#
#        defenses = build_defense_board_players(
#            defense_stats,
#        )
#
#        self.board.extend(defenses)
#
#        self.console.print(
#            f"[green]D/ST ready:[/green] "
#            f"{len(defenses)} units evaluated."
#        )
#
#        self.player_names = {
#            row.espn_id: row.name
#            for row in self.board
#        }
#
#        self.console.print(
#            f"[green]Board ready:[/green] "
#            f"{len(self.board)} draftable assets."
#        )

    def best_available(self):
        our_roster = self.team_rosters.get(self.team_id, [])
        current_overall = self.last_overall_pick + 1
        next_pick = current_overall + self.strategy.rules.league_size

        ranked = self.strategy.rank(
            self.board,
            taken=self.taken,
            our_roster=our_roster,
            overall_pick=current_overall,
            next_pick=next_pick,
            limit=10,
        )

        if not ranked:
            return None, None, []

        player, breakdown = ranked[0]
        return player, breakdown, ranked

    def run(self) -> None:
        if not self.board:
            self.prepare()

        while True:
            try:
                self.console.print(
                    f"[bold]Connecting to ESPN live draft[/bold] "
                    f"league={self.client.league_id} "
                    f"team={self.team_id}"
                )

                with DraftSession(
                    self.client,
                    team_id=self.team_id,
                ) as session:
                    if self.reconnect_count:
                        self.console.print(
                            f"[green]Reconnected to ESPN draft "
                            f"(attempt {self.reconnect_count}).[/green]"
                        )
                    else:
                        self.console.print(
                            "[green]Connected to ESPN draft WebSocket.[/green]"
                        )

                    self._resync_from_rest()

                    completed = self._consume_session(session)

                    if completed:
                        return

            except DraftSessionError as exc:
                self.reconnect_count += 1

                self.console.print()
                self.console.print(
                    f"[bold yellow]ESPN connection lost:[/bold yellow] "
                    f"{exc}"
                )

                self.console.print(
                    f"[yellow]Preserving {len(self.taken)} picks and "
                    f"{len(self.team_rosters.get(self.team_id, []))} "
                    f"players on our roster.[/yellow]"
                )

                if self.pending_selection is not None:
                    name = self.player_names.get(
                        self.pending_selection,
                        str(self.pending_selection),
                    )

                    self.console.print(
                        f"[yellow]Pick status uncertain: "
                        f"{name} ({self.pending_selection}). "
                        f"Will reconcile before selecting again.[/yellow]"
                    )

                delay = min(
                    5.0,
                    0.75 * self.reconnect_count,
                )

                self.console.print(
                    f"[dim]Reconnecting in {delay:.1f}s...[/dim]"
                )

                time.sleep(delay)

    def _consume_session(
        self,
        session: DraftSession,
    ) -> bool:
        for event in session.events():

            if isinstance(event, Init):
                recovered = decode_init_picks(
                    event.payload,
                    league_id=self.client.league_id,
                )

                self._recover_init_picks(recovered)
                continue

            if isinstance(event, AutoDraft):
                if event.team_id == self.team_id:
                    state = (
                        "ON"
                        if event.enabled
                        else "OFF"
                    )

                    self.console.print(
                        f"ESPN timeout autodraft: {state}"
                    )

            elif isinstance(event, Selected):
                self._record_selection(event)

                # Resolve an uncertain SELECT.
                if event.team_id == self.team_id:
                    if self.pending_selection is not None:
                        if event.player_id == self.pending_selection:
                            self.console.print(
                                "[green]Recovered pending pick: "
                                "ESPN accepted our SELECT.[/green]"
                            )
                        else:
                            self.console.print(
                                "[yellow]Pending SELECT was not our "
                                "final pick; ESPN selected another asset."
                                "[/yellow]"
                            )

                        self.pending_selection = None

                our_roster = self.team_rosters.get(
                    self.team_id,
                    [],
                )

                if (
                    len(our_roster)
                    >= self.strategy.rules.roster_size
                ):
                    self._render_final_team()
                    return True

            elif isinstance(event, Selecting):
                if event.team_id != self.team_id:
                    continue

                # This is especially important after reconnect.
                #
                # If ESPN is STILL telling us Team 5 is selecting,
                # our uncertain prior SELECT clearly did not finish
                # the pick.
                if self.pending_selection is not None:
                    name = self.player_names.get(
                        self.pending_selection,
                        str(self.pending_selection),
                    )

                    self.console.print(
                        f"[yellow]ESPN says we are still on the clock; "
                        f"previous SELECT for {name} was not completed."
                        f" Re-ranking now.[/yellow]"
                    )

                    self.pending_selection = None

                self._handle_our_turn(
                    session,
                    event,
                )

        return False

    def _render_final_team(self) -> None:
        roster = self.team_rosters.get(
            self.team_id,
            [],
        )

        self.console.print()
        self.console.rule(
            "[bold green]DRAFT COMPLETE[/bold green]"
        )

        for index, player_id in enumerate(
            roster,
            start=1,
        ):
            name = self.player_names.get(player_id)

            if name is None:
                name = defense_name(player_id)

            if name is None:
                name = f"ESPN player {player_id}"

            self.console.print(
                f"{index:>2}. {name}"
            )

        self.console.print()
        self.console.print(
            f"[bold]Total players: {len(roster)}[/bold]"
        )

    def _record_selection(self, event: Selected) -> None:
        if event.player_id in self.taken:
            return

        self.taken.add(event.player_id)
        self.team_rosters.setdefault(event.team_id, []).append(event.player_id)

        name = self.player_names.get(
            event.player_id,
            f"{defense_name(event.player_id)}",
        )

        suffix = " [bold green]OUR PICK[/bold green]" if event.team_id == self.team_id else ""

        league_size = self.strategy.rules.league_size

        self.last_overall_pick += 1
        overall_pick = self.last_overall_pick

        round_number = (
            (overall_pick - 1) // league_size
        ) + 1

        pick_in_round = (
            (overall_pick - 1) % league_size
        ) + 1

        self.console.print(
            f"R{round_number}.{pick_in_round:02} "
            f"(#{overall_pick:>3}) "
            f"Team {event.team_id:>2} selected "
            f"{name} ({event.player_id})"
            f"{suffix}"
        )

    def _handle_our_turn(
        self,
        session: DraftSession,
        event: Selecting,
    ) -> None:
        candidate, breakdown, ranked = self.best_available()

        if candidate is None:
            self.console.print("[bold red]No available candidate on local board.[/bold red]")
            return

        seconds = event.milliseconds / 1000.0

        self.console.print()
        self.console.print(
            f"[bold green]OUR PICK — {seconds:.0f}s[/bold green]"
        )
        self.console.print(
            f"Recommendation: [bold]{candidate.name}[/bold] "
            f"({candidate.position})"
        )
        self.console.print(
            f"Base: {candidate.board_score:.2f} | "
            f"Live: {breakdown.final_score:.2f}"
        )

        if breakdown.reasons:
            self.console.print(
                "[dim]" + " • ".join(breakdown.reasons[:5]) + "[/dim]"
            )

        self.console.print("[dim]Top alternatives:[/dim]")
        for alt, alt_breakdown in ranked[1:4]:
            self.console.print(
                f"[dim]  {alt.name:<22} "
                f"{alt.position:<3} "
                f"{alt_breakdown.final_score:>6.2f}[/dim]"
            )

        if not self.config.auto_select:
            self.console.print(
                "[yellow]AUTO SELECT IS OFF. ESPN will make its own pick "
                "if the clock expires.[/yellow]"
            )
            return

        # Submit immediately. The valuation work was precomputed before connecting.
        self.console.print(
            f"[cyan]Submitting SELECT {candidate.espn_id}...[/cyan]"
        )

        self.pending_selection = candidate.espn_id

        try:
            ack = session.select_and_wait(
                candidate.espn_id,
                timeout=self.config.ack_timeout,
            )

        except DraftSelectionTimeout as exc:
            self.console.print(
                f"[bold yellow]ACK TIMEOUT: {exc}[/bold yellow]"
            )

            self.console.print(
                "[yellow]Selection remains pending; "
                "waiting for ESPN's live event stream."
                "[/yellow]"
            )

            # DO NOT clear pending_selection.
            return

        except DraftSessionError:
            # DO NOT clear pending_selection.
            #
            # We genuinely don't know whether ESPN accepted the pick.
            # The outer reconnect loop will resolve it.
            raise

        # select_and_wait consumed the matching SELECTED event, so record it here.
        self.pending_selection = None

        self._record_selection(ack)

        self.console.print(
            f"[bold green]ESPN ACKNOWLEDGED: "
            f"{candidate.name}[/bold green]"
        )

    def _resync_from_rest(self) -> int:
        """
        Best-effort authoritative roster reconciliation.

        Returns number of new roster entries discovered.
        Failure is non-fatal because mock-league REST behavior is inconsistent.
        """
        try:
            data = self.client.get_league(
                ["mRoster", "mTeam"]
            )
        except Exception as exc:
            self.console.print(
                f"[dim]REST roster sync unavailable: {exc}[/dim]"
            )
            return 0

        discovered = 0

        for team in data.get("teams", []):
            team_id = team.get("id")

            if team_id is None:
                continue

            roster = team.get("roster", {})
            entries = roster.get("entries", [])

            for entry in entries:
                player_id = entry.get("playerId")

                if not player_id:
                    continue

                if player_id in self.taken:
                    continue

                self.taken.add(player_id)
                self.team_rosters.setdefault(
                    team_id,
                    [],
                ).append(player_id)

                discovered += 1

                name = self.player_names.get(
                    player_id,
                    defense_name(player_id)
                    or f"ESPN player {player_id}",
                )

                self.console.print(
                    f"[dim]RESYNC: Team {team_id} → "
                    f"{name} ({player_id})[/dim]"
                )

        return discovered

    def _recover_init_picks(
        self,
        picks: list[InitPick],
    ) -> None:
        discovered = 0

        for pick in picks:
            self.last_overall_pick = max(
                self.last_overall_pick,
                pick.overall_pick,
            )

            if pick.player_id in self.taken:
                continue

            self.taken.add(
                pick.player_id
            )

            self.team_rosters.setdefault(
                pick.team_id,
                [],
            ).append(
                pick.player_id
            )

            name = self.player_names.get(
                pick.player_id,
                defense_name(pick.player_id)
                or f"ESPN player {pick.player_id}",
            )

            self.console.print(
                f"[dim]RECOVERED "
                f"#{pick.overall_pick:>3} "
                f"Team {pick.team_id:>2} → "
                f"{name}[/dim]"
            )

            discovered += 1

        self.console.print(
            f"[green]INIT reconciliation: "
            f"{discovered} historical picks recovered, "
            f"{len(self.taken)} total known, "
            f"through pick #{self.last_overall_pick}."
            f"[/green]"
        )
