from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHeaderView

from voice_dubbing_app.pages.profiles_page import ProfilesPage
from voice_dubbing_app.view_models import ProfileMode, ProfilePageState

from .gui_helpers import FakeRuntimeClient, application, capability_payload, profile_row
from .helpers import write_pcm_wav


class ProfilesPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = application()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.source = write_pcm_wav(self.base / "nguồn Unicode.wav", seconds=12.0)
        self.client = FakeRuntimeClient()
        self.page = ProfilesPage(self.client)
        self.page.set_capabilities(capability_payload())
        self.ready = profile_row(profile_path=str(self.base / "ready_voice"))
        self.page.set_profiles([self.ready])

    def tearDown(self) -> None:
        self.page.stop_audio()
        self.page.deleteLater()
        self.temporary.cleanup()

    def _valid_new_form(self) -> None:
        self.page.source_edit.setText(str(self.source))
        self.page.name_edit.setText("Giọng thử")
        self.page.consent_check.setChecked(True)

    def _select_first_row(self) -> None:
        self.page.table.selectRow(0)
        self.app.processEvents()

    def test_page_opens_and_selecting_row_does_not_switch_mode(self) -> None:
        self.assertEqual(ProfileMode.CREATE_NEW, self.page.mode)
        self._select_first_row()
        self.assertEqual(ProfileMode.CREATE_NEW, self.page.mode)
        self.assertTrue(self.page.new_mode_radio.isChecked())

    def test_reference_layout_uses_fixed_non_collapsible_workspace(self) -> None:
        self.assertFalse(self.page.splitter.childrenCollapsible())
        self.assertGreaterEqual(self.page.left_scroll.minimumWidth(), 360)
        self.assertGreaterEqual(self.page.right_panel.minimumWidth(), 620)
        self.assertEqual(Qt.ScrollBarAlwaysOff, self.page.left_scroll.horizontalScrollBarPolicy())
        self.assertEqual(Qt.ScrollBarAsNeeded, self.page.left_scroll.verticalScrollBarPolicy())
        self.assertTrue(self.page.review_box.isVisibleTo(self.page))

    def test_profile_table_has_proportional_fixed_columns_and_tooltips(self) -> None:
        self.page.resize(1120, 700)
        self.page.show()
        self.app.processEvents()
        header = self.page.table.horizontalHeader()
        for column in range(self.page.table.columnCount()):
            self.assertEqual(QHeaderView.Fixed, header.sectionResizeMode(column))
        widths = [self.page.table.columnWidth(column) for column in range(7)]
        total = sum(widths)
        for actual, expected in zip(widths, self.page.TABLE_COLUMN_RATIOS, strict=True):
            self.assertAlmostEqual(expected, actual / total, delta=0.025)
        self.assertIn("Profile ID", self.page.table.item(0, 0).toolTip())
        self.assertTrue(self.page.table.item(0, 4).toolTip())

    def test_create_and_update_mode_are_explicitly_isolated(self) -> None:
        self._select_first_row()
        self.page.start_update_selected()
        self.assertEqual(ProfileMode.UPDATE_EXISTING_REFERENCE, self.page.mode)
        self.assertEqual("ready_voice", self.page.update_profile["profile_id"])
        self.page.set_create_mode()
        self.assertEqual(ProfileMode.CREATE_NEW, self.page.mode)
        self.assertIsNone(self.page.update_profile)

    def test_consent_is_required_for_new_cloned_profile(self) -> None:
        self.page.source_edit.setText(str(self.source))
        self.page.name_edit.setText("Không consent")
        self.page.consent_check.setChecked(False)
        self.page.submit_prepare()
        self.assertEqual([], self.client.jobs)
        self.assertIn("quyền", self.page.last_inline_error)

    def test_invalid_manual_reference_range_is_blocked_client_side(self) -> None:
        self._valid_new_form()
        self.page.manual_radio.setChecked(True)
        self.page.manual_start.setValue(0.0)
        self.page.manual_end.setValue(7.999)
        self.page.submit_prepare()
        self.assertEqual([], self.client.jobs)
        self.assertIn("8 đến 15", self.page.last_inline_error)

    def test_update_payload_preserves_identity_and_revision(self) -> None:
        self._select_first_row()
        self.page.start_update_selected()
        self.page.source_edit.setText(str(self.source))
        self.page.manual_radio.setChecked(True)
        self.page.manual_start.setValue(1.0)
        self.page.manual_end.setValue(11.0)
        self.page.submit_prepare()
        job = self.client.jobs[-1]
        self.assertTrue(job["update_existing"])
        self.assertEqual("ready_voice", job["profile_id"])
        self.assertEqual(3, job["expected_profile_revision"])
        self.assertEqual("ready_voice", job["expected_profile_identity"]["profile_id"])
        self.assertNotIn("consent", job)

    def test_auto_suggestion_fills_manual_and_is_guidance_not_fatal(self) -> None:
        self.page.pending_action = "prepare_profile_reference"
        self.page._job_result(
            {
                "_submitted_action": "prepare_profile_reference",
                "status": "success",
                "profile_id": "suggested",
                "preparation_id": "p1",
                "profile_status": "NEEDS_MANUAL_REFERENCE",
                "candidate_status": "PASS",
                "ready_for_commit": False,
                "selection": {"mode": "auto", "start_seconds": 12.25, "end_seconds": 22.25},
                "reference_artifacts": {},
            }
        )
        self.assertEqual(ProfilePageState.AUTO_SUGGESTION_NEEDS_MANUAL, self.page.state)
        self.assertTrue(self.page.manual_radio.isChecked())
        self.assertEqual(12.25, self.page.manual_start.value())
        self.assertEqual(22.25, self.page.manual_end.value())
        self.assertEqual("", self.page.last_inline_error)

    def test_review_receives_both_players_and_commit_uses_exact_artifacts(self) -> None:
        source_mix = write_pcm_wav(self.base / "source mix.wav", seconds=10.0)
        voice_only = write_pcm_wav(self.base / "voice only.wav", seconds=10.0)
        primary = write_pcm_wav(self.base / "primary.wav", seconds=10.0)
        artifacts = {
            "source_mix": {"path": str(source_mix), "sha256": "A"},
            "voice_only": {"path": str(voice_only), "sha256": "B"},
            "primary": {"path": str(primary), "sha256": "B"},
        }
        self.page.pending_action = "prepare_profile_reference"
        self.page._job_result(
            {
                "_submitted_action": "prepare_profile_reference",
                "status": "success",
                "job_id": "prepare-job",
                "preparation_id": "prepare-job",
                "profile_id": "ready_voice",
                "profile_status": "TECHNICAL_PASS_PENDING_LISTENING",
                "candidate_status": "PASS",
                "ready_for_commit": True,
                "selection": {"mode": "manual", "start_seconds": 0.0, "end_seconds": 10.0},
                "reference_artifacts": artifacts,
                "ref_source_mix": artifacts["source_mix"],
                "ref_voice_only": artifacts["voice_only"],
                "reference_validation": {"status": "PASS"},
            }
        )
        self.assertEqual(source_mix.resolve(), self.page.source_player.source_path)
        self.assertEqual(voice_only.resolve(), self.page.voice_player.source_path)
        self.assertFalse(self.page.commit_button.isEnabled())
        self.page.listen_confirm.setChecked(True)
        self.assertFalse(self.page.commit_button.isEnabled())
        self.page.single_speaker_confirm.setChecked(True)
        self.assertTrue(self.page.commit_button.isEnabled())
        self.page.submit_commit()
        self.assertIs(artifacts, self.client.jobs[-1]["reference_artifacts"])

    def test_successful_commit_refreshes_inventory(self) -> None:
        self.page.pending_action = "commit_profile_reference"
        self.page._job_result(
            {
                "_submitted_action": "commit_profile_reference",
                "profile_id": "ready_voice",
                "profile_status": "READY",
            }
        )
        self.assertEqual(ProfilePageState.READY, self.page.state)
        self.assertEqual(1, self.client.refresh_profiles_count)


if __name__ == "__main__":
    unittest.main()
