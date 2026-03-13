"""Unit tests for yas207.bt.protocol (pure functions — no I/O)."""

import pytest

from yas207.bt.protocol import StreamDecoder, encode, parse_state


# ---------------------------------------------------------------------------
# encode()
# ---------------------------------------------------------------------------


class TestEncode:
    def test_status_request(self):
        # ccaa020305f6
        assert encode(bytes.fromhex("0305")) == bytes.fromhex("ccaa020305f6")

    def test_volume_up(self):
        # ccaa0340781e27
        assert encode(bytes.fromhex("40781e")) == bytes.fromhex("ccaa0340781e27")

    def test_input_hdmi(self):
        # ccaa0340784afb
        assert encode(bytes.fromhex("40784a")) == bytes.fromhex("ccaa0340784afb")

    def test_power_off(self):
        # ccaa0340787fc6
        assert encode(bytes.fromhex("40787f")) == bytes.fromhex("ccaa0340787fc6")

    def test_init_payload(self):
        # ccaa090148545320436f6e7453
        assert encode(bytes.fromhex("0148545320436f6e74")) == bytes.fromhex(
            "ccaa090148545320436f6e7453"
        )

    def test_checksum_single_byte(self):
        payload = bytes([0x05])
        pkt = encode(payload)
        assert pkt[-1] == (-(len(payload) + sum(payload))) & 0xFF

    def test_roundtrip_checksum(self):
        """Any payload must encode to a packet whose checksum passes."""
        for hex_payload in ["0305", "40784a", "020001", "0148545320436f6e74"]:
            payload = bytes.fromhex(hex_payload)
            pkt = encode(payload)
            body = pkt[2:-1]  # length byte + payload bytes
            csum = pkt[-1]
            assert (-sum(body)) & 0xFF == csum


# ---------------------------------------------------------------------------
# StreamDecoder
# ---------------------------------------------------------------------------


class TestStreamDecoder:
    def test_single_packet(self):
        raw = bytes.fromhex("ccaa020305f6")
        dec = StreamDecoder()
        dec.feed(raw)
        pkts = dec.pop_packets()
        assert len(pkts) == 1
        assert pkts[0] == bytes.fromhex("0305")

    def test_two_packets_concatenated(self):
        raw = bytes.fromhex("ccaa020305f6") + bytes.fromhex("ccaa0340781e27")
        dec = StreamDecoder()
        dec.feed(raw)
        pkts = dec.pop_packets()
        assert len(pkts) == 2
        assert pkts[0] == bytes.fromhex("0305")
        assert pkts[1] == bytes.fromhex("40781e")

    def test_fragmented_delivery(self):
        raw = bytes.fromhex("ccaa020305f6")
        dec = StreamDecoder()
        dec.feed(raw[:3])
        assert dec.pop_packets() == []
        dec.feed(raw[3:])
        pkts = dec.pop_packets()
        assert len(pkts) == 1
        assert pkts[0] == bytes.fromhex("0305")

    def test_leading_garbage_discarded(self):
        garbage = bytes([0xDE, 0xAD])
        raw = bytes.fromhex("ccaa020305f6")
        dec = StreamDecoder()
        dec.feed(garbage + raw)
        pkts = dec.pop_packets()
        assert len(pkts) == 1

    def test_bad_checksum_skipped(self):
        good = bytes.fromhex("ccaa020305f6")
        bad  = bytes.fromhex("ccaa020305ff")  # wrong checksum
        dec = StreamDecoder()
        dec.feed(bad + good)
        pkts = dec.pop_packets()
        # The bad packet is discarded; the good one should be found
        assert any(p == bytes.fromhex("0305") for p in pkts)

    def test_pop_clears_packets(self):
        dec = StreamDecoder()
        dec.feed(bytes.fromhex("ccaa020305f6"))
        dec.pop_packets()
        assert dec.pop_packets() == []


# ---------------------------------------------------------------------------
# parse_state()
# ---------------------------------------------------------------------------


class TestParseState:
    def _make_payload(
        self,
        power=True,
        input_byte=0x00,   # hdmi
        muted=False,
        volume=22,
        subwoofer=16,
        surround_hi=0x00,
        surround_lo=0x0A,  # tv
        becv=0x00,
    ) -> bytes:
        """Build a synthetic full-status payload (type 0x05, 13 bytes)."""
        return bytes([
            0x05, 0x00,
            0x01 if power else 0x00,
            input_byte,
            0x01 if muted else 0x00,
            volume,
            subwoofer,
            0x20, 0x20, 0x00,
            surround_hi, surround_lo,
            becv,
        ])

    def test_powered_on(self):
        state = parse_state(self._make_payload(power=True))
        assert state is not None
        assert state.power is True

    def test_powered_off(self):
        state = parse_state(self._make_payload(power=False))
        assert state.power is False

    def test_input_hdmi(self):
        state = parse_state(self._make_payload(input_byte=0x00))
        assert state.input == "hdmi"

    def test_input_bluetooth(self):
        state = parse_state(self._make_payload(input_byte=0x05))
        assert state.input == "bluetooth"

    def test_input_tv(self):
        state = parse_state(self._make_payload(input_byte=0x07))
        assert state.input == "tv"

    def test_input_analog(self):
        state = parse_state(self._make_payload(input_byte=0x0C))
        assert state.input == "analog"

    def test_muted(self):
        state = parse_state(self._make_payload(muted=True))
        assert state.muted is True

    def test_volume(self):
        state = parse_state(self._make_payload(volume=22))
        assert state.volume == 22

    def test_subwoofer(self):
        state = parse_state(self._make_payload(subwoofer=16))
        assert state.subwoofer == 16

    def test_surround_tv(self):
        state = parse_state(self._make_payload(surround_hi=0x00, surround_lo=0x0A))
        assert state.surround == "tv"

    def test_surround_stereo(self):
        state = parse_state(self._make_payload(surround_hi=0x01, surround_lo=0x00))
        assert state.surround == "stereo"

    def test_bass_ext_flag(self):
        state = parse_state(self._make_payload(becv=0x20))
        assert state.bass_ext is True
        assert state.clearvoice is False

    def test_clearvoice_flag(self):
        state = parse_state(self._make_payload(becv=0x04))
        assert state.clearvoice is True
        assert state.bass_ext is False

    def test_both_flags(self):
        state = parse_state(self._make_payload(becv=0x24))
        assert state.bass_ext is True
        assert state.clearvoice is True

    def test_non_status_packet_returns_none(self):
        # type byte 0x12 (volume reply) — not a full-status packet
        assert parse_state(bytes([0x12, 0x00, 0x0A])) is None

    def test_too_short_returns_none(self):
        assert parse_state(bytes([0x05, 0x00])) is None

    def test_real_example_from_blog(self):
        # ccaa0d05000100000910202000000a2466 → stripped payload:
        # 05 00 01 00 00 09 10 20 20 00 00 0a 24
        payload = bytes.fromhex("05000100000910202000000a24")
        state = parse_state(payload)
        assert state is not None
        assert state.power is True
        assert state.input == "hdmi"
        assert state.muted is False
        assert state.volume == 9
        assert state.subwoofer == 16
        assert state.surround == "tv"
        assert state.bass_ext is True    # 0x24 & 0x20
        assert state.clearvoice is True  # 0x24 & 0x04


# ---------------------------------------------------------------------------
# parse_packet() — partial state packets
# ---------------------------------------------------------------------------


class TestParsePacket:
    def test_full_status(self):
        payload = bytes.fromhex("05000100000910202000000a24")
        result = parse_packet(payload)
        assert result is not None
        assert result["power"] is True
        assert result["input"] == "hdmi"
        assert result["volume"] == 9
        assert result["surround"] == "tv"

    def test_input_status(self):
        # 0x11 0x00 0x05 (bluetooth)
        result = parse_packet(bytes([0x11, 0x00, 0x05]))
        assert result == {"input": "bluetooth"}

    def test_volume_status(self):
        # 0x12 0x00 0x16 (volume=22)
        result = parse_packet(bytes([0x12, 0x00, 0x16]))
        assert result == {"volume": 22}

    def test_subwoofer_status(self):
        # 0x13 0x00 0x08
        result = parse_packet(bytes([0x13, 0x00, 0x08]))
        assert result == {"subwoofer": 8}

    def test_surround_status(self):
        # 0x15 0x00 0x08 0x20 0x00 (music, bass_ext=on, clearvoice=off)
        result = parse_packet(bytes([0x15, 0x00, 0x08, 0x20, 0x00]))
        assert result["surround"] == "music"
        assert result["bass_ext"] is True
        assert result["clearvoice"] is False

    def test_unknown_type_returns_none(self):
        assert parse_packet(bytes([0x04, 0x00, 0x01])) is None

    def test_empty_returns_none(self):
        assert parse_packet(b"") is None


class TestSoundbarStateMerge:
    def test_merge_partial(self):
        state = SoundbarState(volume=10)
        changed = state.merge({"volume": 11})
        assert changed is True
        assert state.volume == 11

    def test_merge_no_change(self):
        state = SoundbarState(volume=10)
        changed = state.merge({"volume": 10})
        assert changed is False

    def test_is_complete(self):
        state = SoundbarState(
            power=True, input="hdmi", muted=False,
            volume=10, subwoofer=16, surround="music",
            bass_ext=False, clearvoice=False,
        )
        assert state.is_complete() is True

    def test_not_complete(self):
        state = SoundbarState(power=True)
        assert state.is_complete() is False
