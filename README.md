# mediaman

mediaman is a home for a Plex library. Watch your films and shows in the browser, find what to watch next, and ask for what is missing. When the disks fill up, it lists what nobody has watched and clears it away, unless somebody keeps it.

<img src="docs/images/watch.webp" alt="The Watch page: a film to go on with over its own picture, with Resume and Details, and under it a row of films and shows, each with how much is left.">

## Watch

Rest the pointer on a poster and it opens into a preview with the trailer. Everyone has their own place in every title, kept in mediaman and never written to Plex.

<picture>
  <source media="(prefers-reduced-motion: reduce)" srcset="docs/images/preview-still.webp">
  <img src="docs/images/preview.webp" alt="The pointer rests on a film's poster. It grows into a preview with the year, the length, the score and Play, the trailer starts, and it folds back as the pointer leaves.">
</picture>

## Play

Skip the intro, jump from chapter to chapter, or run along the bar to see where you are going. The film comes through mediaman, so the browser never talks to Plex.

<picture>
  <source media="(prefers-reduced-motion: reduce)" srcset="docs/images/player-still.webp">
  <img src="docs/images/player.webp" alt="The player: Skip intro is pressed and the film goes on after its titles. The pointer runs along the bar and a small frame shows each place, with the chapter's name and the time.">
</picture>

When the credits start, it asks how it was, and the next episode is one press away.

<picture>
  <source media="(prefers-reduced-motion: reduce)" srcset="docs/images/credits-still.webp">
  <img src="docs/images/credits.webp" alt="As the credits start, a card asks how the film was: Strong no, Not for me, I like it, I love it. I love it is pressed, and the card says it counts most in your picks.">
</picture>

## Find

Say how long you have tonight, and it offers what fits and you have not seen.

<img src="docs/images/search.webp" alt="The open search: How long have you got tonight, with 2 h chosen and three unseen films that fit, each with its length, when it would end and why. Below are recent searches and phrases to try.">

Or ask in your own words. What you have comes first, then what you could ask for.

<picture>
  <source media="(prefers-reduced-motion: reduce)" srcset="docs/images/ask-still.webp">
  <img src="docs/images/ask.webp" alt="Something kind and funny for a tired evening is typed and asked. Six titles come back, each with one line on why it fits: four in the library, with Play, and two that are not, with Download.">
</picture>

## Picks and ratings

Each person gets picks of their own, each with its reason. Your ratings are yours alone to see, and the picks follow them.

<img src="docs/images/picks.webp" alt="The Recommended for you row, with one pick open: a documentary that is not in the library, with Download and why it was picked: nine people and weather, you watched Northering twice.">

## Collections

Open one film of a series to see the others, and fetch the missing ones together.

<img src="docs/images/collection.webp" width="680" alt="Part of a film's page: its cast, and its collection of four films, three of them in the library, with a button to download the missing one.">

## On the TV

Open `/tv` in the TV's browser and type the code it shows into your phone. While a film plays there, the phone is the remote, and you can lock it or put it away.

<!-- To fill: how a film is sent to a TV from its page (Play on), AirPlay and Chromecast. At this point no page starts one yet. Then tv.webp may be taken again from that button on. -->

<picture>
  <source media="(prefers-reduced-motion: reduce)" srcset="docs/images/tv-still.webp">
  <img src="docs/images/tv.webp" alt="A film on a TV, and beside it a phone with the remote: pause, ten seconds back and forward, volume, chapters and quality. Pause is pressed on the phone and the film pauses on the TV, then goes on.">
</picture>

## Storage

What nobody has watched for a while is listed as leaving, with the day it goes. Anyone can keep a title, from the page or from the weekly email.

<img src="docs/images/leaving.webp" alt="The Leaving soon row: posters with the days they have left, each with Keep under it.">

The meter in the bar shows admins what fills the disks, what is about to go and what went. Scheduled deletion is off until you name the folders it may delete from.

<img src="docs/images/storage.webp" width="430" alt="The storage panel: 307 GB free of 1.5 TB, a bar of movies, TV shows, anime, other files and free space, and a line that twelve scheduled titles will free 135 GB.">

## History

Who watched, asked for and kept what, and what mediaman did by itself. Admins see everyone's; everyone else sees their own.

<img src="docs/images/history.webp" width="680" alt="History: a day's events, such as a film started, one finished, one kept and one downloaded, each with who and when. Above are a search and filters by user, action, title and date.">

## Profile

Choose a picture, link your Plex account, see where you are signed in, and say whether the AI may learn from what you watch.

<img src="docs/images/profile.webp" width="400" alt="The picture dialog of a profile: Upload a photo, or pick one of ten drawn portraits.">

## Run it

```bash
[ -f mediaman.env ] || echo "MEDIAMAN_SECRET_KEY=$(openssl rand -hex 32)" > mediaman.env
docker run -d --name mediaman --env-file mediaman.env -p 8282:8282 -v mediaman-data:/data -v /path/to/your/media:/media rossetv/mediaman
docker exec -it mediaman mediaman-create-user
```

Then open `http://localhost:8282`. The [install guide](docs/install.md) goes from there: another machine, https, Plex and the other services, deletion, TVs, backups and updates.

mediaman is under the [MIT licence](LICENSE). To change it, see [CONTRIBUTING.md](CONTRIBUTING.md); to report a security problem, [SECURITY.md](SECURITY.md).
