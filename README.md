# mkdocs-with-confluence

MkDocs plugin that converts markdown pages into confluence markup
and export it to the Confluence page

## Setup

Install the plugin using pip:

`pip install mkdocs-with-confluence`

Activate the plugin in `mkdocs.yml`:

```yaml
plugins:
  - search
  - mkdocs-with-confluence
```

More information about plugins in the [MkDocs documentation: mkdocs-plugins](https://www.mkdocs.org/user-guide/plugins/).

## Usage

Use following config and adjust it according to your needs:

```yaml
  - mkdocs-with-confluence:
        host_url: https://<YOUR_CONFLUENCE_DOMAIN>/rest/api/content
        space: <YOUR_SPACE>
        parent_page_name: <YOUR_ROOT_PARENT_PAGE>
        username: <YOUR_USERNAME_TO_CONFLUENCE>
        password: <YOUR_PASSWORD_TO_CONFLUENCE>
        enabled_if_env: MKDOCS_TO_CONFLUENCE
        dryrun: true
```

## Customization

- **Skip document upload**: if it possible to skip a document if we provide the "mkdocs_with_confluence_skip" metadata in the header of the document:

    ```
    ---
    mkdocs_with_confluence_skip: true
    ---
    # Some document
    ```

### Requirements

- md2cf
- mimetypes
- mistune
