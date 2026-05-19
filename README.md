README - breadboard v2 
=====================================

*If you have any questions about breadboard, or want to discuss how you're using breadboard for your project, head over to GitHub Discussions by clicking the "Discussions" tab above or going to: [https://github.com/human-nature-lab/breadboard/discussions](https://github.com/human-nature-lab/breadboard/discussions]).*

breadboard is a software platform for developing and conducting human interaction experiments on networks. 

It allows researchers to rapidly design experiments using a flexible domain-specific language and provides researchers with immediate access to a diverse pool of online participants.

Features:

* Experiment logic is expressed in a graph traversal DSL 
* Experiment content is stored in a content management system and edited using a WYSIWYG editor 
* Real-time graph visualization during experiment design and deployment
* High performance bi-directional client-server communication using Netty and WebSockets
* An interactive script window allows the experimenter to quickly make changes to the graph and experiment
* Recruit online participants from Amazon Mechanical Turk using the integrated module

breadboard is built using:

* [The Play Framework](https://www.playframework.com/)
* [Groovy](http://www.groovy-lang.org/) 
* [TinkerPop's](http://tinkerpop.incubator.apache.org/) open source graph computing framework  
* [AngularJS](https://angularjs.org/)
* [D3.js](http://d3js.org/)
* [TinyMCE](http://www.tinymce.com/)
* [CodeMirror](https://codemirror.net/)

Also [Apache Commons](https://commons.apache.org/), [imgscalr](https://github.com/thebuzzmedia/imgscalr), [JUNG](http://jung.sourceforge.net/), [jQuery](https://jquery.com/), [Modernizr](https://modernizr.com/), [Underscore](http://underscorejs.org/), and [Bootstrap](http://getbootstrap.com/).

### Contributing
See the [contributing guide](CONTRIBUTING.md)

### MCP server (this branch)

This branch adds an [MCP](https://modelcontextprotocol.io) server that lets
an LLM agent (e.g. Claude in Claude Code) build, run, and debug Breadboard
experiments through HTTP+JSON — no UI clicking, no `npm run serve` watcher.
It also includes a couple of small fixes in `app/` needed to support that
workflow (deterministic step file ordering, a `file_mode` db evolution).

See **[`mcp-server/README.md`](mcp-server/README.md)** for the tool list
and usage, and **[`mcp-server/DEV_NOTES.md`](mcp-server/DEV_NOTES.md)** for
the working build/run recipe on modern macOS (sbt 0.13.18 + Java 8 +
staged binary; `./start` from the original README does not work without
the `play` activator).
