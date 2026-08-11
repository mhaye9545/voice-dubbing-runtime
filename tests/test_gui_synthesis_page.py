from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from voice_dubbing_app.main_window import MainWindow
from voice_dubbing_app.pages.synthesis_page import SynthesisPage
from voice_dubbing_app.theme import ACCENT, APP_BG
from voice_dubbing_app.view_models import SynthesisPageState

from .gui_helpers import FakeRuntimeClient, application, capability_payload, profile_row
from .helpers import write_pcm_wav


class SynthesisPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = application()

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.client = FakeRuntimeClient()
        self.page = SynthesisPage(self.client)
        self.page.set_capabilities(capability_payload())

    def tearDown(self) -> None:
        self.page.stop_audio()
        self.page.deleteLater()
        self.temporary.cleanup()

    def test_only_ready_profile_can_generate(self) -> None:
        self.page.set_profiles([profile_row(status="NEEDS_MANUAL_REFERENCE")])
        self.page.profile_combo.setCurrentIndex(1)
        self.page.text_edit.setPlainText("Xin chào")
        self.assertFalse(self.page.generate_button.isEnabled())
        self.page.submit_synthesis()
        self.assertEqual([], self.client.jobs)
        self.assertIn("READY", self.page.last_inline_error)

    def test_reference_layout_uses_horizontal_non_collapsible_workspace(self) -> None:
        self.assertFalse(self.page.splitter.childrenCollapsible())
        self.assertGreaterEqual(self.page.settings_box.minimumWidth(), 350)
        self.assertGreaterEqual(self.page.right_panel.minimumWidth(), 620)
        self.assertEqual("CÀI ĐẶT TẠO GIỌNG", self.page.settings_box.title())
        self.assertEqual("VĂN BẢN ĐẦU VÀO", self.page.text_box.title())
        self.assertEqual("KẾT QUẢ", self.page.result_box.title())
        self.page.resize(1120, 700)
        self.page.show()
        self.app.processEvents()
        left, right = self.page.splitter.sizes()
        self.assertGreater(left, 0)
        self.assertGreater(right, 0)
        self.assertGreaterEqual(left / (left + right), 0.32)
        self.assertLessEqual(left / (left + right), 0.37)

    def test_theme_and_empty_audio_state_match_dark_reference_contract(self) -> None:
        stylesheet = self.app.styleSheet()
        self.assertIn(APP_BG, stylesheet)
        self.assertIn(ACCENT, stylesheet)
        self.assertEqual("primary", self.page.generate_button.property("role"))
        self.assertIn("Không có", self.page.result_player.name_label.text())
        self.assertFalse(self.page.result_player.time_label.isVisibleTo(self.page.result_player))

    def test_capabilities_populate_and_unavailable_engine_is_disabled(self) -> None:
        self.assertGreaterEqual(self.page.engine_combo.findData("vixtts_vi"), 0)
        offline = self.page.engine_combo.findData("offline_engine")
        self.assertGreaterEqual(offline, 0)
        self.assertFalse(self.page.engine_combo.model().item(offline).isEnabled())

    def test_language_filter_follows_selected_engine(self) -> None:
        self.page.engine_combo.setCurrentIndex(
            self.page.engine_combo.findData("xtts_v2_multilingual")
        )
        values = [self.page.language_combo.itemData(index) for index in range(self.page.language_combo.count())]
        self.assertEqual(["en", "ko", "zh-cn"], values)
        self.page.engine_combo.setCurrentIndex(self.page.engine_combo.findData("vixtts_vi"))
        values = [self.page.language_combo.itemData(index) for index in range(self.page.language_combo.count())]
        self.assertEqual(["vi"], values)

    def test_empty_text_is_blocked(self) -> None:
        self.page.set_profiles([profile_row()])
        self.page.profile_combo.setCurrentIndex(1)
        self.page.text_edit.clear()
        self.page.submit_synthesis()
        self.assertEqual([], self.client.jobs)
        self.assertIn("Text", self.page.last_inline_error)

    def test_speed_range_is_runtime_contract_range(self) -> None:
        self.assertEqual(0.5, self.page.speed_spin.minimum())
        self.assertEqual(2.0, self.page.speed_spin.maximum())

    def test_keep_model_loaded_only_for_supported_explicit_engine(self) -> None:
        self.page.engine_combo.setCurrentIndex(self.page.engine_combo.findData("auto"))
        self.assertFalse(self.page.keep_loaded_check.isEnabled())
        self.page.engine_combo.setCurrentIndex(self.page.engine_combo.findData("vixtts_vi"))
        self.assertFalse(self.page.keep_loaded_check.isEnabled())
        self.page.engine_combo.setCurrentIndex(
            self.page.engine_combo.findData("xtts_v2_multilingual")
        )
        self.assertTrue(self.page.keep_loaded_check.isEnabled())

    def test_submit_uses_capability_language_and_canonical_keep_field(self) -> None:
        ready = profile_row()
        self.page.set_profiles([ready])
        self.page.profile_combo.setCurrentIndex(1)
        self.page.engine_combo.setCurrentIndex(self.page.engine_combo.findData("vixtts_vi"))
        self.page.text_edit.setPlainText("Xin chào thế giới")
        self.page.submit_synthesis()
        job = self.client.jobs[-1]
        self.assertEqual("vi", job["language"])
        self.assertIn("keep_model_loaded", job)
        self.assertNotIn("keep_model_warm", job)

    def test_result_path_is_shown_and_audio_player_receives_wav(self) -> None:
        output = write_pcm_wav(self.base / "generated.wav", seconds=1.0)
        self.page.pending_action = "synthesize"
        self.page._job_result(
            {
                "_submitted_action": "synthesize",
                "output_audio": str(output),
                "duration_seconds": 1.0,
                "elapsed_seconds": 2.0,
                "peak_ram_gib": 0.5,
            }
        )
        self.assertEqual(SynthesisPageState.RESULT_READY, self.page.state)
        self.assertIn(str(output), self.page.output_label.text())
        self.assertEqual(output.resolve(), self.page.result_player.source_path)

    def test_save_as_copies_generated_wav(self) -> None:
        output = write_pcm_wav(self.base / "generated.wav", seconds=1.0)
        destination = self.base / "đã lưu.wav"
        self.page.output_path = output
        with patch(
            "voice_dubbing_app.pages.synthesis_page.QFileDialog.getSaveFileName",
            return_value=(str(destination), "WAV audio (*.wav)"),
        ):
            self.page.save_as()
        self.assertTrue(destination.is_file())
        self.assertEqual(output.read_bytes(), destination.read_bytes())

    def test_stage_signal_updates_main_window_progress(self) -> None:
        window = MainWindow(self.client, auto_refresh=False)
        try:
            self.client.stage_changed.emit("synthesize", 0.7)
            self.assertEqual(700, window.progress.value())
            self.assertIn("tạo giọng", window.stage_label_widget.text())
        finally:
            window.close()
        self.assertEqual(1, self.client.shutdown_count)
        self.assertEqual(5000, self.client.shutdown_timeout_ms)


if __name__ == "__main__":
    unittest.main()
