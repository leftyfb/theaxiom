"""Generic publication changes refresh the manifest without deployment actions."""
from unittest.mock import Mock, patch

from theaxiom import zim_refresh


def test_reconcile_empty_library_invalidates_without_starting_reader(tmp_path):
    target = tmp_path / "library.xml"
    with (patch.object(zim_refresh.zim_catalog, "render_xml", return_value=b"<library/>"),
          patch.object(zim_refresh.zim_catalog, "manifest_path", return_value=target),
          patch.object(zim_refresh.zim_catalog, "write_manifest") as write,
          patch.object(zim_refresh.zim_reader_probe, "acknowledge") as ack):
        assert zim_refresh.reconcile(lambda: [], attempts=1)
        write.assert_called_once_with([])
        ack.assert_not_called()


def test_reconcile_retries_after_kiwix_updates_library(tmp_path):
    target = tmp_path / "library.xml"
    target.write_bytes(b"<library><book id='ready'/></library>")
    ack = Mock(side_effect=[ValueError("not loaded"), None])
    with (patch.object(zim_refresh.zim_catalog, "render_xml", return_value=target.read_bytes()),
          patch.object(zim_refresh.zim_catalog, "manifest_path", return_value=target),
          patch.object(zim_refresh.zim_reader_probe, "acknowledge", ack),
          patch.object(zim_refresh.time, "sleep") as sleep):
        assert zim_refresh.reconcile(lambda: ["ready"], attempts=3, interval=0.01)
        assert ack.call_count == 2
        sleep.assert_called_once_with(0.01)


def test_reconcile_reader_missing_returns_pending_not_published(tmp_path):
    target = tmp_path / "library.xml"
    target.write_bytes(b"<library><book id='ready'/></library>")
    with (patch.object(zim_refresh.zim_catalog, "render_xml", return_value=target.read_bytes()),
          patch.object(zim_refresh.zim_catalog, "manifest_path", return_value=target),
          patch.object(zim_refresh.zim_reader_probe, "acknowledge", side_effect=OSError("absent")),
          patch.object(zim_refresh.time, "sleep")):
        assert not zim_refresh.reconcile(lambda: ["ready"], attempts=3, interval=0)
