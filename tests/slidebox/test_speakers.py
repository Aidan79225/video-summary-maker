from slidebox.domain.entities import Cue, SpeakerTurn
from slidebox.usecases.speakers import OVERLAP, UNKNOWN, attach_speakers


def test_cues_are_assigned_only_when_one_speaker_clearly_dominates():
    cues = (
        Cue(0.0, 4.0, "甲"),
        Cue(4.0, 8.0, "交疊"),
        Cue(8.0, 10.0, "沒有聲音"),
    )
    turns = (
        SpeakerTurn(0.0, 3.0, "說話者 1"),
        SpeakerTurn(3.0, 6.0, "說話者 2"),
        SpeakerTurn(6.0, 8.0, "說話者 1"),
    )

    labeled, speakers, uncertain = attach_speakers(cues, turns)

    assert [cue.speaker for cue in labeled] == ["說話者 1", OVERLAP, UNKNOWN]
    assert speakers == ("說話者 1", "說話者 2")
    assert uncertain == 2


def test_a_tiny_boundary_overlap_is_not_enough_to_assign_a_cue():
    labeled, _, uncertain = attach_speakers(
        (Cue(0.0, 10.0, "長句"),),
        (SpeakerTurn(0.0, 0.1, "說話者 1"),),
    )

    assert labeled[0].speaker == UNKNOWN
    assert uncertain == 1
