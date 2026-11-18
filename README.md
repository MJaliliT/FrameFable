# FrameFable

A local web panel for the CSCE 880 project *From Images to Children’s Stories*. The core app uses Python 3.10 or later and runs with the standard library alone. Grounding DINO, MusicGen, OpenAI narration and PDF export require optional Python dependencies.

## Start the panel

From the project directory, run:

```sh
./run.sh
```

Open the URL printed by the script and keep the terminal running while using the panel. The script tries port 4173 and automatically picks a free port if needed. To specify a preferred port, run `PORT=5000 ./run.sh`. The script uses the project’s `.venv` if available.

## VPS deployment

For a subdomain on an existing VPS, see the [deployment guide](deploy/README.md).

## Enable Grounding DINO for bounding boxes

Use Python 3.12 to create a local environment, then install and download the model once.

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-detector.txt
.venv/bin/python detector.py
./run.sh
```

If the environment and checkpoint already exist, use just the final command. The model weights are stored in `.models/`. Detection runs on the computer and the detector makes no network requests during analysis. The selected OpenAI or Ollama model still identifies subjects and writes the story. Grounding DINO uses each subject’s label to find its location and uses a rough initial box only to distinguish similar instances. It omits ambiguous or low-confidence boxes. If the detector is unavailable, the panel falls back to the vision model’s boxes. [Grounding DINO Tiny model card](https://huggingface.co/IDEA-Research/grounding-dino-tiny)

## Enable background music

MusicGen Small runs locally for noncommercial use, with no music-service key or per-track fee. It uses the same Python dependencies as Grounding DINO. If you already set up Grounding DINO, run only the model download and launcher commands.

```sh
.venv/bin/python -m pip install -r requirements-detector.txt
.venv/bin/python soundtrack.py
./run.sh
```

The checkpoint download is about 2.36 GB. The panel generates a 20-second instrumental loop based on the story’s theme and tone before showing the story, then starts the loop automatically. **Mute music** silences the soundtrack. The music continues at lower volume during narration and stops when you leave the story page. Music generation runs on this computer and can take several minutes. [MusicGen Small model card](https://huggingface.co/facebook/musicgen-small)

## Enable OpenAI narration

Install the audio dependency into the same environment used by the launcher.

```sh
.venv/bin/python -m pip install -r requirements-audio.txt
./run.sh
```

In an OpenAI story, **Play** sends the story text to OpenAI for AI-generated narration. The first playback can take a moment. Pause and replay use the audio already loaded in the tab. The additional audio request can incur OpenAI usage charges. [OpenAI Realtime audio guide](https://developers.openai.com/api/docs/guides/realtime-conversations)

## Enable story PDF downloads

Install the PDF dependency in the Python environment used by the launcher.

```sh
.venv/bin/python -m pip install -r requirements-pdf.txt
./run.sh
```

**Download story PDF** saves the title, story preferences, text, and images in reading order, including inserted bridge illustrations. The export is created locally.

## Set up a provider

1. Open **Settings**.
2. For **OpenAI**, paste your API key into Settings or make it available to the server as `OPENAI_API_KEY`. The default vision model is the draft’s GPT-6.1 Terra. Use **Load vision models** to see recognized vision model IDs available to your key, or enter a model ID manually. Images go to OpenAI for visual analysis. Optional bridge illustrations use GPT-Image-2.6 Flare and require image-generation access. [OpenAI vision documentation](https://developers.openai.com/api/docs/guides/images-vision), [OpenAI image generation documentation](https://developers.openai.com/api/docs/guides/image-generation)
3. For **Ollama**, run the local Ollama service, install a vision-capable model that supports structured output, and use **Load installed models** to select it. Point the app at your machine’s loopback Ollama URL, which defaults to `http://127.0.0.1:11434`. [Ollama vision documentation](https://docs.ollama.com/capabilities/vision), [Ollama structured output documentation](https://docs.ollama.com/capabilities/structured-outputs)
4. Select **Test connection** to send a small built-in image through the selected model. Then select **Save settings**.

The browser holds the API key in tab memory and forwards it to the local backend only for OpenAI requests. The app stores completed run metadata in the current browser. Source images, names and stories are held in memory until you refresh or close the tab. Download any story you want to keep.

## Run a story case

- Upload 1–20 JPG, PNG or WebP images and drag thumbnails to reorder them. The browser prepares JPEG copies with a maximum edge of 1,200 pixels for analysis.
- Pick the tone, reading level and approximate length.
- Analyze the images, then name any subjects you want the story to use. The model links recurring subjects automatically. The panel tracks local box detection separately from the fallback vision refinement. An empty name delegates naming to the story model.
- Request connecting scenes and use any proposed bridge as prose. With OpenAI illustrations enabled, generating a bridge illustration adds it to the image sequence and final story. Use **Remove illustration** to keep the bridge as prose. Illustrations use descriptions of the adjacent scenes, so visual continuity can vary.
- Generate the story and download the illustrated PDF. Use **Generate another story** to reuse the same setup or **Start over** to clear the workspace while keeping provider settings and completed run history. To try a new tone or reading level with the same images, return to setup, change the preferences, and revisit the later stages. Image analysis is cached per image and model in the current tab.
- Refresh before starting a new evaluation case if you want fresh measurements. This clears the workspace and provider settings while keeping the completed run history in the browser.

## Research exports

- **Run summaries** contains one row per completed story with input settings, cache reuse, bridge generation and insertion, timing per stage, reported token totals, and the sum of available cost estimates.
- **Stage details** contains the individual model calls, including reported usage and failed attempts since the previous completed story or provider/model change. Timing sums cover request time and exclude user review time. Cached analysis adds no new model usage. The bridge-generation stage is measured separately.
- **Rating template** provides 30 rows for the 10 cases and three researchers. Enter a score from 1 (strongly disagree) to 5 (strongly agree) for each of the draft’s seven criteria.

The app estimates cost from reported usage and standard rates for GPT-6.1 Terra, GPT-5.4 mini, and its configured bridge image model. Missing usage or unsupported pricing produces an empty estimate. The CSV includes a priced-call count so partial totals are identifiable. [OpenAI API pricing](https://developers.openai.com/api/docs/pricing), [GPT-5.4 mini pricing](https://developers.openai.com/api/docs/models/gpt-5.4-mini)

## Verification

Run `python3 -B -m unittest -v` for provider payload, usage, error and HTTP checks with simulated responses. The existing interface was previously browser-tested with the 1-image and 20-image workflows on both simulated providers. It also covered recurring names, bridge illustration insertion, model selection and exports. The live Ollama service was accessible and the app rejected an installed text-only model. Live story generation requires an OpenAI API key or an installed Ollama vision model.
