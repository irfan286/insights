# aaPanel vhost patch — unblock claude.ai Connectors (OAuth) on insights.707.co.id

Target: `/www/server/panel/vhost/nginx/insights.707.co.id.conf`

Frappe v16 is already fully configured for this — `enable_dynamic_client_registration`,
`show_protected_resource_metadata` and `show_auth_server_metadata` are all `1`, so
claude.ai registers itself and no OAuth Client has to be created by hand. The only thing
in the way is this vhost.

---

## Change 1 — stop rewriting the Host header

`get_resource_url()` in `frappe/integrations/oauth2.py` is documented as *"Uses request
URL to reflect the resource URL"* and reads `frappe.request.url` — **not** `host_name` from
site config. The vhost currently tells Frappe every request arrived at `127.0.0.1`, so the
advertised metadata URL is `http://127.0.0.1/.well-known/oauth-protected-resource` and an
OAuth client follows it to port 80 on its own machine.

```diff
     location ^~ / {
       proxy_pass http://127.0.0.1:8088;
-      proxy_set_header Host 127.0.0.1;
+      proxy_set_header Host $host;
```

**Why this is safe:** the container's nginx sets `proxy_set_header X-Frappe-Site-Name
frontend;` explicitly, and its single server block is the default server, so site
resolution never depended on the incoming Host in the first place.

## Change 2 — tell Frappe the request was HTTPS

There is no `X-Forwarded-Proto` header today, so Frappe builds `http://` URLs for a site
served over TLS. OAuth clients reject non-HTTPS metadata.

```diff
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
+      proxy_set_header X-Forwarded-Proto $scheme;
       proxy_set_header REMOTE-HOST $remote_addr;
```

## Change 3 — let the OAuth discovery paths reach the app

aaPanel serves `location /.well-known` from `/www/wwwroot/insights.707.co.id` for Let's
Encrypt ACME challenges, which swallows the OAuth discovery endpoints. Confirmed:
`/.well-known/oauth-protected-resource` returns 200 from inside the container and fails
from outside.

Add this **as a new block**, leaving the ACME one untouched. nginx checks regex locations
after prefix matching when the winning prefix is not `^~`, and `/.well-known` is a plain
prefix — so this takes priority for these paths only, and ACME keeps working.

```nginx
    # OAuth 2.0 discovery (RFC 9728 / RFC 8414) must reach Frappe, not the ACME
    # webroot. Narrower than the /.well-known block below on purpose: certificate
    # renewal still serves from the filesystem.
    location ~ ^/\.well-known/(oauth-authorization-server|oauth-protected-resource|openid-configuration) {
        proxy_pass http://127.0.0.1:8088;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
    }
```

---

## Applying safely

```bash
V=/www/server/panel/vhost/nginx/insights.707.co.id.conf
sudo cp $V $V.bak-oauth                 # rollback point
# ...edit...
sudo nginx -t                           # MUST pass before reloading
sudo nginx -s reload                    # reload, not restart -- no dropped connections
```

`nginx -t` is the gate. A vhost that fails it takes down every site on this host, not just
Insights, so never reload without it passing.

**Rollback:** `sudo cp $V.bak-oauth $V && sudo nginx -t && sudo nginx -s reload`

## Verifying afterwards

```bash
curl -si https://insights.707.co.id/api/method/insights.mcp.handle_mcp | grep -i www-authenticate
# expect: resource_metadata="https://insights.707.co.id/.well-known/oauth-protected-resource"
#         (https, real hostname, no port)

curl -s https://insights.707.co.id/.well-known/oauth-protected-resource
curl -s https://insights.707.co.id/.well-known/oauth-authorization-server
# both expect: JSON, not 404 and not the ACME webroot
```

Only when the first command echoes the real HTTPS hostname will claude.ai's Connectors
flow get past discovery.

## What is NOT needed

- Creating an OAuth Client by hand — `enable_dynamic_client_registration = 1`, claude.ai
  registers itself.
- Any change to Frappe, the image, or the compose stack.
- `host_name` in site config — it is already `https://insights.707.co.id`, and
  `get_resource_url()` ignores it regardless.
