# ComfyUI PNG to WebP converter — correct EXIF format for drag-and-drop workflow loading

Finding a script that converts ComfyUI PNGs to WebP with working metadata is very difficult. There are a handful of attempts floating around GitHub but they all fail silently. You drag the WebP into ComfyUI and nothing loads or the WebP has been stripped of the proper metadata for ComfyUI to utilize for loading workflows. After a lot of digging it turns out ComfyUI's WebP parser is really specific about which EXIF fields the data goes in, how it's formatted, and what other fields are allowed nearby. Most scripts get at least one of those wrong.

This script is my attempt at getting it right. It reads the workflow and prompt data from the PNG, writes it to the correct EXIF fields, and verifies the metadata survived before doing anything permanent to your files. Runs on all your cores so it's reasonably fast on big folders too.

## HOW TO USE:

  This script will utilize every logical processor available for mass conversion of PNGs.  
  You can lower the max number of threads at the end of the script. Search for `Entry point`
  to locate the max worker count.

  Place this script in the directory containing your ComfyUI PNG outputs and run it.
  On startup it will ask whether to delete the original PNGs after conversion.
  - Yes: WebP is saved alongside the PNG, then PNG is deleted after verified conversion
  - No:  WebP is saved to a "webp_out" subdirectory (original PNGs untouched)
  Already-converted files are skipped (delete the webp to re-convert).
  Conversion runs in parallel across all available logical CPU cores.
  
## REQUIREMENTS:

`pip install Pillow piexif`

## What ComfyUI's pnginfo.ts actually requires:

  1. TWO EXIF fields, both ASCII (type 2), written CONSECUTIVELY with nothing between them:
       IFD0 tag 0x010E  ImageDescription  →  "workflow:{json}"
       IFD0 tag 0x010F  Make              →  "prompt:{json}"

  2. The value format is EXACTLY  "<key>:<json>"  — no space after the colon,
     no newlines inside the JSON.  ComfyUI splits on the FIRST colon to get key/value.

  3. NOTHING else may live in IFD0 between those two tags.
     piexif will sort IFD0 by tag number (0x010E < 0x010F) so they will be adjacent
     as long as you don't add other IFD0 tags.

  4. Any non-ASCII EXIF entry (UTF-16 XPComment, Exif IFD UserComment, etc.) causes
     ComfyUI's parser to add `undefined` to its result object, then crash with
     TypeError when it tries to call .indexOf(':') on it.  That is the silent failure
     that killed every previous attempt that included extra fields.

  5. ComfyUI cannot load lossless WebP (bug in pnginfo.js / app.js as of mid-2024).
     Keep WEBP_LOSSLESS = False.
     
... I want to note that figuring this all out was a royal pain in the butt.

## Final Thoughts

I initially created this script without any LLM help, but getting the EXIF data locked down was
becoming incredibly frustrating.  I worked with Claude Code to analyze the ComfyUI technical
docs to figure out where I was making mistakes, and it found the issue.

**What ComfyUI's pnginfo.ts actually requires**
*TWO EXIF fields, both ASCII (type 2), written CONSECUTIVELY with nothing between them*

Sure enough, after making some tweaks, that resolved it.

I've tested it extensively, and so far it's been 100% successful
at converting PNG to WMF while maintaining the proper metadata.
