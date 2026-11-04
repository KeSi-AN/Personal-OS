# Connecting Google Calendar and Gmail (read-only)

Personal OS reads **today's calendar** and the **sender and subject of unread mail**, for briefings and questions. It uses two scopes:

- `calendar.readonly`
- `gmail.metadata`: headers only. Google won't give this client message bodies at all.

Nothing fetched is stored, and the sign-in token is kept in Windows Credential Manager. Google needs you to create your own OAuth client once (about 10 minutes).

## 1. Create a project and turn on the two APIs

1. Open [console.cloud.google.com](https://console.cloud.google.com/) with the Google account you want to connect.
2. Create a project (top bar → project picker → **New project**), e.g. "Personal OS".
3. **APIs & Services → Library**: search for and **Enable** both **Google Calendar API** and **Gmail API**.

## 2. Set up the consent screen

In **Google Auth Platform** (or **APIs & Services → OAuth consent screen** in the older layout):

1. **Branding:** app name "Personal OS"; your email as the support and developer contact.
2. **Audience:** **External**, then under **Test users** add your own Gmail address.
3. **Data access → Add or remove scopes:** add `.../auth/calendar.readonly` and `.../auth/gmail.metadata`.

**About "Testing" mode:** while the app is in *Testing*, Google expires its sign-ins after **7 days**, and you'd need to reconnect weekly. To avoid that, choose **Audience → Publish app**. For an app only you use, Google lets you continue past its *"Google hasn't verified this app"* screen (**Advanced → Go to Personal OS**). You're the developer and the only user.

## 3. Create the client and download it

1. **Clients → Create client → Application type: Desktop app**; name it "Personal OS".
2. Click **Download JSON**. You get a file like `client_secret_1234-abc.apps.googleusercontent.com.json`.

## 4. Connect

```powershell
uv run personal-os google connect "C:\Users\you\Downloads\client_secret_....json"
```

Your browser opens Google's sign-in. Choose the account, allow the two read-only permissions, and the tab says *Connected*. Then:

```powershell
uv run personal-os google status       # "Connected. Today: N calendar event(s); M unread …"
uv run personal-os briefing            # now includes calendar and mail
```

`connect` copies the JSON into `%LOCALAPPDATA%\PersonalOS\google\`, so you can delete the downloaded file afterwards.

## Disconnecting

```powershell
uv run personal-os google disconnect   # revokes access at Google and deletes the token here
```

You can also remove access at [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
