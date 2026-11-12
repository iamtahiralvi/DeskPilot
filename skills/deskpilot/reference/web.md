# Websites

## Contents
- Which browser you are reading
- Opening and navigating
- Gmail
- X (Twitter)
- GitHub
- Forms and uploads

## Which browser you are reading

- **The user's everyday Chrome, Edge or Brave**: read through Windows accessibility (`e` refs). Only the visible
  part of the page is listed, so scroll to reach more. This needs no setup and never shows a prompt.
- **The DeskPilot browser** (`launch("agent browser https://…")`): a separate Chrome profile read through DevTools
  (`b` refs), including elements outside the visible area or inside a scrolling list, which are scrolled into view
  before a click (see [dropdowns-and-screenshots.md](dropdowns-and-screenshots.md)).
  Use it when precision matters and the user doesn't need their own logins. The user signs in there once.
- If "Allow remote debugging?" appears, never click it. The user decides.

## Opening and navigating

1. `snapshot()` first. `page <url>` tells you where the active tab is.
2. If the URL is already right, don't navigate.
3. `launch("https://site/path")` reuses a tab that already shows that page (it ignores `www.`, a trailing `/`
   and `#anchors`), otherwise opens it.
4. To change the URL of the current tab, click the address bar, then type:
   `{"click":"Address and search bar"},{"key":"ctrl+a"},{"type":"github.com/owner/repo","enter":true}`.
   Ctrl+L can fail when the page holds focus in a text box.
5. After navigating, `{"wait": "<text on the new page>"}`, then find elements again: old refs are stale.

## Gmail

- Inbox rows are `row` elements: `snapshot(filter=",")` lists them with sender, subject and time.
- Open an email by clicking its subject link. The body often appears truncated in the snapshot. Read it with
  `screenshot(ref=<body element>)`, which crops sharply and costs few tokens.
- Next and previous email: the `Older` and `Newer` buttons in the email view.
- Drafting a reply: click `Reply`, type into the message body, and stop before `Send` unless told to send.

## X (Twitter)

- Find someone's post: `x.com/search?q=from%3A<handle>%20<words>&f=live`.
- A post's own page has a `Post your reply` box under it. Typing into that box is safe; the `Reply` button posts.
- In chats, the box is `edit "Message"`; Enter sends, so never add `"enter": true` there unless told to send.
- A quoted post is a card inside another post. Click its text to open the original.
- X captures many keys. Prefer clicking targets to keyboard shortcuts.

## GitHub

- Repository pages list files as links. `find(text="README")` beats scrolling.
- For anything that changes a repository (merge, delete, settings), prefer the `gh` command line if it is
  available. It is precise and leaves a record.

## Forms and uploads

- Fill every field in one `act`, then verify with `find` before submitting.
- File pickers are Windows dialogs. Type the full path into `File name` and press Enter.
- Wait for uploads with `{"wait":"Uploading","gone":true,"timeout":90}`.
- Submitting, posting, paying or sending needs the user's OK. DeskPilot may show them an Allow/Deny dialog.
