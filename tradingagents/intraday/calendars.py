"""Exchange sessions backed by pandas-market-calendars and a crypto 24/7 clock."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from importlib.metadata import PackageNotFoundError, version

UTC = UTC


@dataclass(frozen=True, slots=True)
class TradingSession:
    session_date: date
    open_utc: datetime
    close_utc: datetime
    calendar_name: str


class SessionCalendar:
    """Calendar adapter. Equity schedules are supplied by pandas-market-calendars."""

    def __init__(self, name: str):
        self.name = name.upper()
        self._calendar = None
        self._session_cache: dict[tuple[date, date], list[TradingSession]] = {}
        if self.name != "24/7":
            try:
                import pandas_market_calendars as mcal

                self._calendar = mcal.get_calendar(self.name)
                self.provider_version = version("pandas-market-calendars")
                self.provenance = f"pandas-market-calendars/{self.provider_version}:{self.name}"
            except (ImportError, PackageNotFoundError) as exc:
                raise RuntimeError(
                    "pandas-market-calendars is required for equity sessions"
                ) from exc
            except Exception as exc:
                raise ValueError(f"unsupported session calendar {self.name!r}") from exc
        else:
            self.provider_version = "builtin-1"
            self.provenance = "builtin-UTC-midnight-24/7-v1"

    def sessions(self, start: date, end: date) -> list[TradingSession]:
        if end < start:
            raise ValueError("end date must not precede start date")
        cache_key = (start, end)
        if cache_key in self._session_cache:
            return self._session_cache[cache_key]
        if self.name == "24/7":
            out = []
            day = start
            while day <= end:
                op = datetime.combine(day, time.min, UTC)
                out.append(TradingSession(day, op, op + timedelta(days=1), self.name))
                day += timedelta(days=1)
            self._session_cache[cache_key] = out
            return out
        try:
            schedule = self._calendar.schedule(
                start_date=start.isoformat(), end_date=end.isoformat()
            )
        except Exception as exc:
            raise ValueError(
                f"calendar {self.name} could not resolve {start}..{end}: {exc}"
            ) from exc
        result = []
        for session_date, row in schedule.iterrows():
            op = row["market_open"].to_pydatetime().astimezone(UTC)
            close = row["market_close"].to_pydatetime().astimezone(UTC)
            result.append(TradingSession(session_date.date(), op, close, self.name))
        self._session_cache[cache_key] = result
        return result

    def session_for_bar(
        self, timestamp: datetime, duration: timedelta = timedelta(minutes=1)
    ) -> TradingSession | None:
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("bar timestamp must be timezone-aware")
        stamp = timestamp.astimezone(UTC)
        if self.name == "24/7":
            day = stamp.date()
            op = datetime.combine(day, time.min, UTC)
            return TradingSession(day, op, op + timedelta(days=1), self.name)
        local_day = stamp.astimezone(self._calendar.tz).date()
        for day in (local_day - timedelta(days=1), local_day, local_day + timedelta(days=1)):
            entries = self.sessions(day, day)
            if entries:
                session = entries[0]
                if session.open_utc <= stamp and stamp + duration <= session.close_utc:
                    return session
        return None

    def expected_bar_starts(
        self, session: TradingSession, interval_minutes: int = 1
    ) -> list[datetime]:
        if interval_minutes <= 0:
            raise ValueError("interval_minutes must be positive")
        step = timedelta(minutes=interval_minutes)
        out = []
        cursor = session.open_utc
        while cursor + step <= session.close_utc:
            out.append(cursor)
            cursor += step
        return out

    @property
    def timezone(self) -> str:
        if self.name == "24/7":
            return "UTC"
        return str(self._calendar.tz)
