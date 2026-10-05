from typing import Any

Look = dict[str, Any]

LID_ROTATIONS: Look = {
    "left_upper_lid_rotation_deg": 0,
    "right_upper_lid_rotation_deg": 0,
    "left_lower_lid_rotation_deg": 0,
    "right_lower_lid_rotation_deg": 0,
}

STANDBY_EYES: Look = {
    "accessory": "none",
    "auto_blink": True,
    "gaze_x": 0.0,
    "gaze_y": 0.0,
    "spacing": 1.0,
    "scale": 1.0,
    "scale_x": 2.0,
    "scale_y": 2.0,
    "left_upper_lid_y": -80,
    "right_upper_lid_y": -80,
    "left_lower_lid_y": 80,
    "right_lower_lid_y": 80,
    **LID_ROTATIONS,
}

# 眼睑遮罩覆盖原有眼睛，自定义矢量图案位于独立图层。
HIDDEN_EYES: Look = {
    "accessory": "custom_vector",
    "custom_accessory_layer": "front",
    "accessory_scale": 1.0,
    "accessory_x": 0.0,
    "accessory_y": 0.0,
    "auto_blink": False,
    "gaze_x": 0.0,
    "gaze_y": 0.0,
    "scale": 0.5,
    "scale_x": 1.0,
    "scale_y": 1.0,
    "left_upper_lid_y": 0,
    "right_upper_lid_y": 0,
    "left_lower_lid_y": 0,
    "right_lower_lid_y": 0,
    **LID_ROTATIONS,
    "color": "#FFFFFF",
    "transition_ms": 0,
}


def look(
    wide: float,
    tall: float,
    spacing: float,
    *,
    roundness: float = 1.0,
    upper_lid: int = -80,
    lower_lid: int = 80,
    droop: int = 0,
) -> Look:
    # 沿用机器人原有的白色眼睛，只调整大小、间距、圆角和眼睑。
    return STANDBY_EYES | {
        "color": "#FFFFFF",
        "roundness": roundness,
        "spacing": spacing,
        "scale_x": wide,
        "scale_y": tall,
        "left_upper_lid_y": upper_lid,
        "right_upper_lid_y": upper_lid,
        "left_lower_lid_y": lower_lid,
        "right_lower_lid_y": lower_lid,
        "left_upper_lid_rotation_deg": droop,
        "right_upper_lid_rotation_deg": -droop,
    }


LOOKS: tuple[tuple[str, Look], ...] = (
    ("豆豆眼：两颗小圆点，离得很开", look(1.0, 1.0, 1.4)),
    ("竖椭圆眼：细高的椭圆，间距偏宽", look(1.0, 1.7, 1.3)),
    ("圆角方眼：小号的圆角方块，间距偏宽", look(1.2, 1.2, 1.3, roundness=0.5)),
    ("大圆眼：大圆眼睛，离得很开", look(1.7, 1.7, 1.3)),
    ("扁圆眼：横向的扁椭圆，间距偏宽", look(1.7, 1.0, 1.2)),
    ("半睁眼：上眼睑压下来，懵懵的", look(1.5, 1.5, 1.2, upper_lid=-15)),
    ("笑眼：下眼睑抬起来，弯成笑眼", look(1.5, 1.5, 1.2, lower_lid=10)),
    ("无辜眼：上眼睑向一侧倾斜", look(1.5, 1.5, 1.2, upper_lid=-30, droop=12)),
    ("无辜眼（反向）：上眼睑向另一侧倾斜", look(1.5, 1.5, 1.2, upper_lid=-30, droop=-12)),
)
