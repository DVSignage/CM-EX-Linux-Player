from cmx.wall import crop_filter_normalised, crop_filter_pixel, normalise_crop, stream_url_with_buffers


def test_pixel_crop_with_canvas_is_normalised():
    assert normalise_crop({"x": 960, "y": 0, "w": 960, "h": 1080, "canvas_w": 1920, "canvas_h": 1080}) == \
        (0.5, 0.0, 0.5, 1.0)


def test_already_normalised_crop_is_kept():
    assert normalise_crop({"x": 0.25, "y": 0.5, "w": 0.25, "h": 0.5}) == (0.25, 0.5, 0.25, 0.5)


def test_empty_full_or_missing_crop_is_none():
    assert normalise_crop(None) is None
    assert normalise_crop({"x": 0, "y": 0, "w": 0, "h": 1}) is None
    assert normalise_crop({"x": 0, "y": 0, "w": 1, "h": 1}) is None


def test_normalised_filter_string():
    assert crop_filter_normalised((0.5, 0.0, 0.5, 1.0)) == "crop=w=iw*0.5:h=ih*1:x=iw*0.5:y=ih*0,setsar=1"
    assert crop_filter_normalised(None) is None


def test_pixel_filter_matches_the_old_player():
    crop = {"x": 960, "y": 0, "w": 960, "h": 1080, "canvas_w": 1920, "canvas_h": 1080}
    assert crop_filter_pixel(crop) == \
        "scale=1920:1080:force_original_aspect_ratio=disable,setsar=1,crop=960:1080:960:0"
    assert crop_filter_pixel({"x": 1, "y": 2, "w": 3, "h": 4}) == "crop=3:4:1:2"
    assert crop_filter_pixel({}) is None


def test_stream_url_buffers():
    assert stream_url_with_buffers("udp://239.0.0.1:5000").startswith("udp://239.0.0.1:5000?buffer_size=")
    assert "&buffer_size=" in stream_url_with_buffers("udp://h:1?x=1")
