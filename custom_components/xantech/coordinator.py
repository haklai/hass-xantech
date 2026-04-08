"""DataUpdateCoordinator for Xantech Multi-Zone Amplifier."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import AMP_TYPE_SONANCE6, DEFAULT_SCAN_INTERVAL, DOMAIN

if TYPE_CHECKING:
    from pyxantech import AmpControlBase

LOG = logging.getLogger(__name__)


class XantechCoordinator(DataUpdateCoordinator[dict[int, dict[str, Any]]]):
    """Coordinator to manage fetching zone statuses from the amplifier."""

    @staticmethod
    def _coerce_zone_status(status: Any) -> dict[str, Any] | None:
        """Coerce a pyxantech zone status into a plain dict."""
        if status is None:
            return None
        if isinstance(status, dict):
            return status if status else None
        for attr in ("dict", "as_dict", "to_dict"):
            fn = getattr(status, attr, None)
            if callable(fn):
                try:
                    data = fn()
                except Exception:  # noqa: BLE001
                    return None
                if isinstance(data, dict) and data:
                    return data
        return None

    def __init__(
        self,
        hass: HomeAssistant,
        amp: AmpControlBase,
        amp_name: str,
        amp_type: str,
        zone_ids: list[int],
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialize the coordinator.

        Args:
            hass: Home Assistant instance
            amp: The pyxantech amplifier controller
            amp_name: Friendly name of the amplifier
            amp_type: Amplifier type string (e.g. 'sonance6', 'xantech8')
            zone_ids: List of zone IDs to poll
            scan_interval: Polling interval in seconds
        """
        super().__init__(
            hass,
            LOG,
            name=f'{DOMAIN}_{amp_name}',
            update_interval=timedelta(seconds=scan_interval),
        )
        self.amp = amp
        self.amp_name = amp_name
        self.amp_type = amp_type
        self.zone_ids = zone_ids
        self._consecutive_errors = 0
        self._max_consecutive_errors = 5

    def _get_proto(self) -> Any:
        """Find the underlying protocol object from the amp controller."""
        proto = getattr(self.amp, "protocol", None) or getattr(self.amp, "_protocol", None)
        if proto is None:
            for attr in ("_amp", "_client", "_serial", "_transport", "_conn", "_connection"):
                candidate = getattr(self.amp, attr, None)
                if candidate is not None:
                    proto = candidate
                    break
        if proto is None:
            LOG.debug('No protocol object found on amp (type=%s)', type(self.amp).__name__)
        return proto

    async def _async_protocol_request(self, proto: Any, command: str) -> str | None:
        """Send a raw command via pyxantech protocol and return decoded response."""
        request_bytes = command.encode("ascii", errors="ignore")
        for method_name in ("send_command", "request", "send", "write", "query"):
            fn = getattr(proto, method_name, None)
            if not callable(fn):
                continue
            try:
                result = await fn(request_bytes)
            except TypeError:
                try:
                    result = fn(request_bytes)
                except Exception:  # noqa: BLE001
                    continue
            except Exception:  # noqa: BLE001
                continue
            if result is None:
                continue
            if isinstance(result, (bytes, bytearray)):
                return bytes(result).decode("ascii", errors="ignore")
            if isinstance(result, str):
                return result
            if isinstance(result, list) and result:
                first = result[0]
                if isinstance(first, (bytes, bytearray)):
                    return bytes(first).decode("ascii", errors="ignore")
                if isinstance(first, str):
                    return first
            try:
                return str(result)
            except Exception:  # noqa: BLE001
                return None
        return None

    @staticmethod
    def _parse_sonance_tagged_response(resp: str, zone_id: int, kind: str) -> int | None:
        """Parse Sonance C4630 responses like '+V453', '+G320', '+S12', '+L1-5'.

        Format is '+<kind><zone_digit><value>' where value may be signed.
        Zone is always a single digit (1-6).
        """
        token = f"+{kind}"
        idx = resp.find(token)
        if idx == -1:
            return None
        tail = resp[idx + len(token):]
        if not tail:
            return None
        if tail[0] != str(int(zone_id)):
            return None
        value_str = tail[1:]
        if not value_str:
            return None
        try:
            return int(value_str.strip())
        except ValueError:
            return None

    async def _async_try_read_zone_volume(self, zone_id: int) -> int | None:
        """Read zone volume via raw Sonance protocol (:Vx? / :Gx?).

        Sonance :Zx? status query omits volume; it must be queried separately.
        """
        proto = self._get_proto()
        if proto is None:
            return None
        for kind, cmd in (("V", f":V{zone_id}?\r"), ("G", f":G{zone_id}?\r")):
            resp = await self._async_protocol_request(proto, cmd)
            if resp:
                parsed = self._parse_sonance_tagged_response(resp, zone_id=zone_id, kind=kind)
                if parsed is not None:
                    return parsed
        return None

    async def _async_try_read_zone_source(self, zone_id: int) -> int | None:
        """Read zone source via raw Sonance protocol (:Sx?).

        Sonance :Zx? status query omits source; it must be queried separately.
        """
        proto = self._get_proto()
        if proto is None:
            return None
        resp = await self._async_protocol_request(proto, f":S{zone_id}?\r")
        if resp:
            return self._parse_sonance_tagged_response(resp, zone_id=zone_id, kind="S")
        return None

    async def _async_update_data(self) -> dict[int, dict[str, Any]]:
        """Fetch data from the amplifier for all zones.

        Returns:
            Dictionary mapping zone_id to zone status dict
        """
        zone_statuses: dict[int, dict[str, Any]] = {}

        try:
            for zone_id in self.zone_ids:
                try:
                    raw_status = await self.amp.zone_status(zone_id)
                    status = self._coerce_zone_status(raw_status)
                    if not status:
                        LOG.debug('No status returned for zone %d', zone_id)
                        continue

                    if self.amp_type == AMP_TYPE_SONANCE6 and status.get("power"):
                        # Sonance :Zx? omits volume — query explicitly
                        if status.get("volume") == 0:
                            read_volume = await self._async_try_read_zone_volume(zone_id)
                            LOG.debug('Zone %d explicit volume read: %r', zone_id, read_volume)
                            if isinstance(read_volume, int):
                                status["volume"] = read_volume

                        # Sonance :Zx? omits source — query explicitly
                        read_source = await self._async_try_read_zone_source(zone_id)
                        LOG.debug('Zone %d explicit source read: %r', zone_id, read_source)
                        if isinstance(read_source, int):
                            status["source"] = read_source

                        # Sonance :Zx? omits bass/treble/balance — query explicitly
                        read_bass = await self._async_try_read_zone_bass(zone_id)
                        LOG.debug('Zone %d explicit bass read: %r', zone_id, read_bass)
                        if isinstance(read_bass, int):
                            status["bass"] = read_bass

                        read_treble = await self._async_try_read_zone_treble(zone_id)
                        LOG.debug('Zone %d explicit treble read: %r', zone_id, read_treble)
                        if isinstance(read_treble, int):
                            status["treble"] = read_treble

                        read_balance = await self._async_try_read_zone_balance(zone_id)
                        LOG.debug('Zone %d explicit balance read: %r', zone_id, read_balance)
                        if isinstance(read_balance, int):
                            status["balance"] = read_balance

                    zone_statuses[zone_id] = status
                except Exception:
                    LOG.warning(
                        'Failed to get status for zone %d', zone_id, exc_info=True
                    )
                    # continue with other zones even if one fails

            # reset error counter on success
            self._consecutive_errors = 0

            LOG.debug('Updated %d zones for %s', len(zone_statuses), self.amp_name)
            return zone_statuses

        except Exception as err:
            self._consecutive_errors += 1
            if self._consecutive_errors >= self._max_consecutive_errors:
                LOG.error(
                    'Failed to update %s after %d attempts',
                    self.amp_name,
                    self._consecutive_errors,
                    exc_info=err,
                )
            raise UpdateFailed(
                f'Error communicating with {self.amp_name}: {err}'
            ) from err

    async def async_set_zone_power(self, zone_id: int, power: bool) -> None:
        """Set power state for a zone."""
        try:
            await self.amp.set_power(zone_id, power)
            await self.async_request_refresh()
        except Exception:
            LOG.exception('Failed to set power for zone %d', zone_id)
            raise

    async def async_set_zone_source(self, zone_id: int, source_id: int) -> None:
        """Set source for a zone."""
        try:
            await self.amp.set_source(zone_id, source_id)
            await self.async_request_refresh()
        except Exception:
            LOG.exception('Failed to set source for zone %d', zone_id)
            raise

    async def async_set_zone_volume(self, zone_id: int, volume: int) -> None:
        """Set volume for a zone (0-38 scale)."""
        try:
            await self.amp.set_volume(zone_id, volume)
            await self.async_request_refresh()
        except Exception:
            LOG.exception('Failed to set volume for zone %d', zone_id)
            raise

    async def async_set_zone_mute(self, zone_id: int, mute: bool) -> None:
        """Set mute state for a zone."""
        try:
            await self.amp.set_mute(zone_id, mute)
            await self.async_request_refresh()
        except Exception:
            LOG.exception('Failed to set mute for zone %d', zone_id)
            raise

    async def _async_try_read_zone_bass(self, zone_id: int) -> int | None:
        """Read zone bass via raw Sonance protocol (:Lx? -> +L<zone><value>)."""
        proto = self._get_proto()
        if proto is None:
            return None
        resp = await self._async_protocol_request(proto, f":L{zone_id}?\r")
        LOG.debug('Zone %d bass proto resp: %r', zone_id, resp)
        if resp:
            return self._parse_sonance_tagged_response(resp, zone_id=zone_id, kind="L")
        return None

    async def _async_try_read_zone_treble(self, zone_id: int) -> int | None:
        """Read zone treble via raw Sonance protocol (:Hx? -> +H<zone><value>)."""
        proto = self._get_proto()
        if proto is None:
            return None
        resp = await self._async_protocol_request(proto, f":H{zone_id}?\r")
        LOG.debug('Zone %d treble proto resp: %r', zone_id, resp)
        if resp:
            return self._parse_sonance_tagged_response(resp, zone_id=zone_id, kind="H")
        return None

    async def _async_try_read_zone_balance(self, zone_id: int) -> int | None:
        """Read zone balance via raw Sonance protocol (:Bx? -> +B<zone><value>)."""
        proto = self._get_proto()
        if proto is None:
            return None
        resp = await self._async_protocol_request(proto, f":B{zone_id}?\r")
        LOG.debug('Zone %d balance proto resp: %r', zone_id, resp)
        if resp:
            return self._parse_sonance_tagged_response(resp, zone_id=zone_id, kind="B")
        return None

    async def _async_send_sonance_cmd(self, cmd: str) -> None:
        """Send a raw Sonance command. Raises on +ERR response."""
        proto = self._get_proto()
        if proto is None:
            raise RuntimeError('No protocol object available for Sonance command')
        resp = await self._async_protocol_request(proto, cmd)
        LOG.debug('Sonance cmd %r -> %r', cmd.strip(), resp)
        if resp and "+ERR" in resp:
            raise RuntimeError(f'Sonance cmd {cmd.strip()!r} returned error: {resp!r}')

    async def async_set_zone_bass(self, zone_id: int, bass: int) -> None:
        """Set bass level for a zone."""
        try:
            if self.amp_type == AMP_TYPE_SONANCE6:
                # pyxantech clamps negatives and zero-pads incorrectly for Sonance
                await self._async_send_sonance_cmd(f":L{zone_id}{bass:+03d}\r")
            else:
                await self.amp.set_bass(zone_id, bass)
            await self.async_request_refresh()
        except Exception:
            LOG.exception('Failed to set bass for zone %d', zone_id)
            raise

    async def async_set_zone_treble(self, zone_id: int, treble: int) -> None:
        """Set treble level for a zone."""
        try:
            if self.amp_type == AMP_TYPE_SONANCE6:
                await self._async_send_sonance_cmd(f":H{zone_id}{treble:+03d}\r")
            else:
                await self.amp.set_treble(zone_id, treble)
            await self.async_request_refresh()
        except Exception:
            LOG.exception('Failed to set treble for zone %d', zone_id)
            raise

    async def async_set_zone_balance(self, zone_id: int, balance: int) -> None:
        """Set balance for a zone."""
        try:
            if self.amp_type == AMP_TYPE_SONANCE6:
                await self._async_send_sonance_cmd(f":B{zone_id}{balance:+03d}\r")
            else:
                await self.amp.set_balance(zone_id, balance)
            await self.async_request_refresh()
        except Exception:
            LOG.exception('Failed to set balance for zone %d', zone_id)
            raise

    async def async_get_zone_snapshot(self, zone_id: int) -> dict[str, Any] | None:
        """Get a snapshot of zone status for later restoration."""
        try:
            return await self.amp.zone_status(zone_id)
        except Exception:
            LOG.exception('Failed to snapshot zone %d', zone_id)
            raise

    async def async_restore_zone(self, snapshot: dict[str, Any]) -> None:
        """Restore a zone from a snapshot."""
        try:
            await self.amp.restore_zone(snapshot)
            await self.async_request_refresh()
        except Exception:
            LOG.exception('Failed to restore zone')
            raise
