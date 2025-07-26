ToDo:
- A 3rd button during the confirm/deny prompt 'edit' could be use to solicit user input on the name, in the case that there is an extra character, or misspelling
- Notification of what has just been downloaded to the other users on the `allowed_user_ids` in `config.ini`
- A cancel button for the multi-magnet selection
- A cancel button for torrents currently downloading
- look into deleting entire chat every time to reducing remote logging
- bug with the display of magnet links, does not refer to the codec
- latest commit broke torrent downloading interaction, nothing happened after submitting a link

- auto-search, type a movie or tv show, below is the site heirarchy, but there is more tribal knowledge to dump here before implementation
  - movies
    - yts.mx
    - 1337x.to
    -thepiratebay.org
  - tv
    - eztvx.to
    -1337x.to

      - For movies or tv yts.mx makes things easy, but for 1337x.to and thepiratebay.org will require some prefernces to narrow the list to a reasonable length
        - movies
          - This may require a secondary query to the user for 1080 or 4k
          - 1080p minimum
          - x265 format
          - Blueray preference
        - tv
          - 1080 minimum
          - x265 preference
          - seeders: EZTV, ELITE, MeGusta, 

- multi-download support or at the very least queueing