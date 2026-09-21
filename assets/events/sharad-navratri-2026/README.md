# Navratri custom image candidates

Upload the following JPEG files here (minimum width and height: 600 pixels):

1. `01-shailaputri.jpg` — October 11, 2026
2. `02-brahmacharini.jpg` — October 12
3. `03-chandraghanta.jpg` — October 13
4. `04-kushmanda.jpg` — October 14
5. `05-skandamata.jpg` — October 15
6. `06-katyayani.jpg` — October 16
7. `07-kalaratri.jpg` — October 17
8. `08-mahagauri.jpg` — October 18
9. `09-siddhidatri.jpg` — October 19

These are source assets, not automatically approved daily images. Commit and push
them before running the image workflow for the applicable date. `events[].days[].image_path`
in config.json maps each date to its file. The event must be enabled.

The image workflow validates the custom image with the same rules as temple
images and adds it to the administrator's review candidates alongside that day's
normal temple sources. Missing, corrupt or undersized files are skipped and logged.
The administrator must select one candidate before publication when
`admin.require_image_approval` is enabled (the production setting). An existing
approved selection remains preserved on reruns.

Source assets stay here; generated review previews use the existing docs/images
pipeline. The normal dated-image cleanup does not remove these reusable assets.
Custom event previews append a grey footer measuring 24% of the original image
height, containing VIP Seva, www.vipseva.com and date/source details. The artwork
is not cropped, resized or overlaid. Admin previews this branded version; approval
publishes those exact bytes without applying a second footer. JPEG encoding may
introduce small compression differences; original uploaded files remain untouched.
Menu/page visibility flags do not disable image candidates. In config.json,
`events[].enabled` controls the entire event (images, menu and page content).
`events[].images_enabled` controls only custom image candidates; it defaults to
true when omitted and is explicitly false for this event until images are wanted.
`events[].days[].image_path` selects that day's local artwork file. A missing path
means no custom image for that day. Both enabled flags must be true to load it.
Set images_enabled to false to retain shlokas while offering only normal temple
images. Rerun collection to refresh already pending candidates; existing approved
selections remain preserved. Upload original artwork you can reuse;
no placeholder JPEG files are supplied.
