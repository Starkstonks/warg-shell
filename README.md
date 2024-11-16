# DigitalOcean Warg

This project allows you to take control of a DigitalOcean PaaS terminal from
your real terminal, making the experience a lot better than just using an
emulator from the JS side.

There is an authentication protocol to be documented.

## Usage

First, you need to authenticate against your Warg server. Do:

```bash
uvx warg-shell auth <your-domain> <your-token>
```

Then you can use any component that you want:

```bash
uvx warg-shell shell <your-domain> <your-product> <your-env> <your-component>
```
