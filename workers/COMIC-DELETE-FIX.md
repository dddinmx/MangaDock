# Comic deletion and detail appearance

The original detail form sends its CSRF token in the POST body. The authentication adapter constructed another Request from the original request, transferring its body. The subsequent mutation input reader failed with HTTP 500. JSON tests supplied a CSRF header and missed this path. A matching original URL-encoded form regression failed with 500 before the fix and passed with 303 after it. Read-only staging logs also recorded `/comic/delete` returning 500.

Authentication now constructs a bodyless request from the URL, method and copied headers, preserving the original mutation body while retaining origin/session/CSRF checks. Regression coverage includes URL-encoded and multipart forms, invalid-token rejection without deletion, and a committed deletion with 101 R2 objects while Queue admission is paused. Remaining garbage plans survive admission pauses for maintenance recovery; a fully cleaned deletion sends no redundant Queue message.

At the user's request, the comic detail chapter list shows only chapter titles, and dark backgrounds use neutral grey surfaces instead of blue-black. Shared background tokens, translucent surfaces, mobile home/header gradients and browser/PWA theme colours are updated. Local browser acceptance submits the actual original delete form, checks title-only chapters and the neutral dark detail background. Deployed browser acceptance creates and removes only random-ID staging fixtures; it does not delete the user's 大奉打更人 or other library content.

The staging library limit remains 9 GB. Existing database and Queue guards remain. Original desktop/mobile screenshot parity, private R2 reading and saved progress still run in CI. Production has not switched.
