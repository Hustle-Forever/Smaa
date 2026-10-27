import { Config } from "@remotion/cli/config";

// Codec / encoding defaults. Each renderMedia() call can override.
Config.setVideoImageFormat("jpeg");
Config.setCodec("h264");
