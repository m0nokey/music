settings.server.telnet := true
settings.server.telnet.bind_addr := "0.0.0.0"
settings.server.telnet.port := 1234
settings.server.telnet.revdns := false

next_track = request.queue(
  id="next_track",
  interactive=true,
  prefetch=1
)

music = playlist(
  id="radio_music",
  mode="randomize",
  loop=true,
  reload_mode="watch",
  max_fail=100,
  "/music"
)

radio_source = fallback(
  id="radio",
  track_sensitive=false,
  [next_track, music]
)

add_skip_command(radio_source)

radio = mksafe(radio_source)

output.icecast(
  %fdkaac(
    channels=2,
    samplerate=44100,
    bitrate=128,
    aot="mpeg4_aac_lc",
    transmux="adts"
  ),
  host="icecast-kh",
  port=8000,
  user="source",
  password="__ICECAST_SOURCE_PASSWORD__",
  mount="radio.aac",
  name="Radio",
  description="Internet Radio",
  genre="Music",
  public=false,
  send_icy_metadata=true,
  radio
)

hls_aac =
  %ffmpeg(
    format="mpegts",
    %audio(
      codec="aac",
      channels=2,
      ar=44100,
      b="128k"
    )
  ).{
    id3_version=4,
    replay_id3=true
  }

output.file.hls(
  id="radio_hls",
  playlist="index.m3u8",
  segment_duration=2.0,
  segments=15,
  segments_overhead=5,
  persist_at="/hls/state.config",
  "/hls",
  [("aac", hls_aac)],
  radio
)
