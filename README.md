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

Open `/tv` in the TV's browser and type the code it shows into your phone. That TV is then yours: press Play on, on a film's page or in the player, and choose it. While that TV plays, the phone is its remote, and you can lock it or put it away.

Play on also lists AirPlay and Chromecast, which need no pairing:

- AirPlay: Safari on an iPhone, iPad or Mac, and a TV with AirPlay, such as an Apple TV. The player must stay open: leaving it stops the film.
- Chromecast: Chrome, or a browser built on it, on a computer or an Android device, mediaman opened over https, and a TV with Chromecast or Google Cast. It is off until an admin turns on Allow Chromecast in Settings, under TVs & Casting, since it loads Google's Cast script into mediaman's pages. The [install guide](docs/install.md#chromecast) says what that lets in.

Either way the TV must be able to open mediaman's address. TVs too old for a browser are added by an admin, see [TVs](docs/install.md#tvs).

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

Admins see who watched, asked for and kept what, and what mediaman deleted and scanned. Everyone else sees only what they did themselves.

<img src="docs/images/history.webp" width="680" alt="History: a day's events, such as a film started, one finished, one kept and one downloaded, each with who and when. Above are a search and filters by user, action, title and date.">

## Profile

Choose a picture, change your own email and password, link your Plex account (what you play in Plex apps then shapes your picks), see where you are signed in and sign out your other devices, rename or unpair your TVs, and say whether the AI may use your searches, ratings and requests and how your plays went.

<img src="docs/images/profile.webp" width="400" alt="The picture dialog of a profile: Upload a photo, or pick one of ten drawn portraits.">

## Run it

In a new folder, make a `docker-compose.yml`, with your own folder in place of `/path/to/your/media`, mounted at the path Plex uses for it:

```yaml
services:
  mediaman:
    image: rossetv/mediaman
    container_name: mediaman
    restart: unless-stopped
    ports:
      - "8282:8282"
    environment:
      MEDIAMAN_SECRET_KEY: ${MEDIAMAN_SECRET_KEY:?run the line that makes .env}
    volumes:
      - mediaman-data:/data
      - /path/to/your/media:/media

volumes:
  mediaman-data:
    name: mediaman-data
```

Then, in that folder, make the key, start [`rossetv/mediaman`](https://hub.docker.com/r/rossetv/mediaman) and create the first admin:

```bash
[ -f .env ] || (umask 077 && echo "MEDIAMAN_SECRET_KEY=$(openssl rand -hex 32)" > .env)
docker compose up -d
docker compose exec mediaman mediaman-create-user
```

Keep `.env`: your settings cannot be read without its key. Open `http://localhost:8282` in Chrome or Firefox; for Safari or another machine, see [Sign in](docs/install.md#sign-in). Then [connect Plex and the rest](docs/install.md#connect-your-services). The guide also covers https, deletion, TVs, backups and updates, and [moving from `docker run`](docs/install.md#moving-from-docker-run).

mediaman is under the [MIT licence](LICENSE). To change it, see [CONTRIBUTING.md](CONTRIBUTING.md); to report a security problem, [SECURITY.md](SECURITY.md).
