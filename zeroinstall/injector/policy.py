# Copyright (C) 2006, Thomas Leonard
# See the README file for details, or visit http://0install.net.

import time
import sys, os
from logging import info, debug, warn
import arch

from model import *
import basedir
from namespaces import *
import ConfigParser
import reader
from iface_cache import iface_cache
from zeroinstall import NeedDownload

path_dirs = os.environ.get('PATH', '/bin:/usr/bin').split(':')
def _available_in_path(command):
	for x in path_dirs:
		if os.path.isfile(os.path.join(x, command)):
			return True
	return False

class _Cook:
	"""A Cook follows a Recipe."""
	# Maybe we're taking this metaphor too far?

	def __init__(self, policy, required_digest, recipe, force = False):
		"""Start downloading all the ingredients."""
		self.recipe = recipe
		self.required_digest = required_digest
		self.downloads = {}
		self.streams = {}

		for step in recipe.steps:
			dl = policy.begin_archive_download(step, success_callback = 
				lambda stream, step=step: self.ingredient_ready(step, stream),
				force = force)
			self.downloads[step] = dl
		self.test_done()
	
	def ingredient_ready(self, step, stream):
		assert step not in self.streams
		self.streams[step] = stream
		del self.downloads[step]
		self.test_done()
	
	def test_done(self):
		if self.downloads: return

		from zeroinstall.zerostore import unpack

		store = iface_cache.stores.stores[0]
		tmpdir = store.get_tmp_dir_for(self.required_digest)
		try:
			for step in self.recipe.steps:
				unpack.unpack_archive(step.url, self.streams[step], tmpdir, step.extract)
			store.check_manifest_and_rename(self.required_digest, tmpdir)
			tmpdir = None
		finally:
			if tmpdir is not None:
				shutil.rmtree(tmpdir)

class Policy(object):
	__slots__ = ['root', 'implementation', 'watchers',
		     'help_with_testing', 'network_use',
		     'freshness', 'ready', 'handler', 'warned_offline']

	def __init__(self, root, handler = None):
		self.watchers = []
		self.help_with_testing = False
		self.network_use = network_full
		self.freshness = 60 * 60 * 24 * 30	# Seconds allowed since last update (1 month)
		self.ready = False

		# If we need to download something but can't because we are offline,
		# warn the user. But only the first time.
		self.warned_offline = False

		# (allow self for backwards compat)
		self.handler = handler or self

		debug("Supported systems: '%s'", arch.os_ranks)
		debug("Supported processors: '%s'", arch.machine_ranks)

		path = basedir.load_first_config(config_site, config_prog, 'global')
		if path:
			try:
				config = ConfigParser.ConfigParser()
				config.read(path)
				self.help_with_testing = config.getboolean('global',
								'help_with_testing')
				self.network_use = config.get('global', 'network_use')
				self.freshness = int(config.get('global', 'freshness'))
				assert self.network_use in network_levels
			except Exception, ex:
				warn("Error loading config: %s", ex)

		self.set_root(root)

		iface_cache.add_watcher(self)
	
	def set_root(self, root):
		assert isinstance(root, (str, unicode))
		self.root = root
		self.implementation = {}		# Interface -> [Implementation | None]

	def save_config(self):
		config = ConfigParser.ConfigParser()
		config.add_section('global')

		config.set('global', 'help_with_testing', self.help_with_testing)
		config.set('global', 'network_use', self.network_use)
		config.set('global', 'freshness', self.freshness)

		path = basedir.save_config_path(config_site, config_prog)
		path = os.path.join(path, 'global')
		config.write(file(path + '.new', 'w'))
		os.rename(path + '.new', path)
	
	def recalculate(self):
		self.implementation = {}
		self.ready = True
		debug("Recalculate! root = %s", self.root)
		def process(dep):
			iface = self.get_interface(dep.interface)
			if iface in self.implementation:
				debug("cycle; skipping second %s", iface)
				return
			self.implementation[iface] = None	# Avoid cycles

			impl = self._get_best_implementation(iface, dep.restrictions)
			if impl:
				debug("Will use implementation %s (version %s)", impl, impl.get_version())
				self.implementation[iface] = impl
				for d in impl.dependencies.values():
					debug("Considering dependency %s", d)
					process(d)
			else:
				debug("No implementation chould be chosen yet");
				self.ready = False
		process(Dependency(self.root))
		for w in self.watchers: w()
	
	# Only to be called from recalculate, as it is quite slow.
	# Use the results stored in self.implementation instead.
	def _get_best_implementation(self, iface, restrictions):
		impls = iface.implementations.values()
		for f in self.usable_feeds(iface):
			debug("Processing feed %s", f)
			try:
				feed_iface = self.get_interface(f.uri)
				if feed_iface.name and iface.uri not in feed_iface.feed_for:
					warn("Missing <feed-for> for '%s' in '%s'",
						iface.uri, f.uri)
				if feed_iface.implementations:
					impls.extend(feed_iface.implementations.values())
			except NeedDownload, ex:
				raise ex
			except Exception, ex:
				warn("Failed to load feed %s for %s: %s",
					f, iface, str(ex))

		debug("get_best_implementation(%s), with feeds: %s", iface, iface.feeds)

		if not impls:
			info("Interface %s has no implementations!", iface)
			return None
		for r in restrictions:
			impls = filter(r.meets_restriction, impls)
		best = impls[0]
		for x in impls[1:]:
			if self.compare(iface, x, best) < 0:
				best = x
		if self.is_unusable(best):
			info("Best implementation of %s is %s, but unusable (%s)", iface, best,
							self.get_unusable_reason(best))
			return None
		return best
	
	def compare(self, interface, b, a):
		a_stab = a.get_stability()
		b_stab = b.get_stability()

		# Usable ones come first
		r = cmp(self.is_unusable(b), self.is_unusable(a))
		if r: return r

		# Preferred versions come first
		r = cmp(a_stab == preferred, b_stab == preferred)
		if r: return r

		if self.network_use != network_full:
			r = cmp(self.get_cached(a), self.get_cached(b))
			if r: return r

		# Stability
		stab_policy = interface.stability_policy
		if not stab_policy:
			if self.help_with_testing: stab_policy = testing
			else: stab_policy = stable

		if a_stab >= stab_policy: a_stab = preferred
		if b_stab >= stab_policy: b_stab = preferred

		r = cmp(a_stab, b_stab)
		if r: return r
		
		# Newer versions come before older ones
		r = cmp(a.version, b.version)
		if r: return r

		# Get best OS
		r = cmp(arch.os_ranks.get(a.os, None),
			arch.os_ranks.get(b.os, None))
		if r: return r

		# Get best machine
		r = cmp(arch.machine_ranks.get(a.machine, None),
			arch.machine_ranks.get(b.machine, None))
		if r: return r

		# Slightly prefer cached versions
		if self.network_use == network_full:
			r = cmp(self.get_cached(a), self.get_cached(b))
			if r: return r

		return cmp(a.id, b.id)
	
	def usable_feeds(self, iface):
		"""Generator for iface.feeds that are valid for our architecture."""
		for f in iface.feeds:
			if f.os in arch.os_ranks and f.machine in arch.machine_ranks:
				yield f
			else:
				debug("Skipping '%s'; unsupported architecture %s-%s",
					f, f.os, f.machine)
	
	def get_ranked_implementations(self, iface):
		impls = iface.implementations.values()
		for f in self.usable_feeds(iface):
			feed_iface = self.get_interface(f.uri)
			if feed_iface.implementations:
				impls.extend(feed_iface.implementations.values())
		impls.sort(lambda a, b: self.compare(iface, a, b))
		return impls
	
	def is_unusable(self, impl):
		return self.get_unusable_reason(impl) != None

	def get_unusable_reason(self, impl):
		"""Returns the reason why this impl is unusable, or None if it's OK"""
		stability = impl.get_stability()
		if stability <= buggy:
			return stability.name
		if self.network_use == network_offline and not self.get_cached(impl):
			return "Not cached and we are off-line"
		if impl.os not in arch.os_ranks:
			return "Unsupported OS"
		if impl.machine not in arch.machine_ranks:
			return "Unsupported machine type"
		return None

	def get_interface(self, uri):
		iface = iface_cache.get_interface(uri)

		if iface.last_modified is None:
			if self.network_use != network_offline:
				debug("Interface not cached and not off-line. Downloading...")
				self.begin_iface_download(iface)
			else:
				if self.warned_offline:
					debug("Nothing known about interface, but we are off-line.")
				else:
					if iface.feeds:
						info("Nothing known about interface '%s' and off-line. Trying feeds only.", uri)
					else:
						warn("Nothing known about interface '%s', but we are in off-line mode "
							"(so not fetching).", uri)
						self.warned_offline = True
		elif not uri.startswith('/'):
			staleness = time.time() - (iface.last_checked or 0)
			debug("Staleness for %s is %.2f hours", iface, staleness / 3600.0)

			if self.network_use != network_offline and self.freshness > 0 and staleness > self.freshness:
				debug("Updating %s", iface)
				self.begin_iface_download(iface, False)
		#else: debug("Local interface, so not checking staleness.")

		return iface
	
	def begin_iface_download(self, interface, force = False):
		debug("begin_iface_download %s (force = %d)", interface, force)
		if interface.uri.startswith('/'):
			return
		debug("Need to download")
		dl = self.handler.get_download(interface.uri, force = force)
		if dl.on_success:
			# Possibly we should handle this better, but it's unlikely anyone will need
			# to use an interface as an icon or implementation as well, and some of the code
			# assumes it's OK keep asking for the same interface to be downloaded.
			info("Already have a handler for %s; not adding another", interface)
			return
		dl.on_success.append(lambda stream: 
			iface_cache.check_signed_data(interface, stream, self.handler))
	
	def begin_impl_download(self, impl, retrieval_method, force = False):
		"""Start fetching impl, using retrieval_method. Each download started
		will call monitor_download."""
		assert impl
		assert retrieval_method

		if isinstance(retrieval_method, DownloadSource):
			def archive_ready(stream):
				iface_cache.add_to_cache(retrieval_method, stream)
			self.begin_archive_download(retrieval_method, success_callback = archive_ready, force = force)
		elif isinstance(retrieval_method, Recipe):
			_Cook(self, impl.id, retrieval_method)
		else:
			raise Exception("Unknown download type for '%s'" % retrieval_method)

	def begin_archive_download(self, download_source, success_callback, force = False):
		if download_source.url.endswith('.rpm'):
			if not _available_in_path('rpm2cpio'):
				raise SafeException("The URL '%s' looks like an RPM, but you don't have the rpm2cpio command "
						"I need to extract it. Install the 'rpm' package first (this works even if "
						"you're on a non-RPM-based distribution such as Debian)." % download_source.url)
		dl = self.handler.get_download(download_source.url, force = force)
		dl.expected_size = download_source.size
		dl.on_success.append(success_callback)
		return dl
	
	def begin_icon_download(self, interface, force = False):
		debug("begin_icon_download %s (force = %d)", interface, force)

		# Find a suitable icon to download
		for icon in interface.get_metadata(XMLNS_IFACE, 'icon'):
			type = icon.getAttribute('type')
			if type != 'image/png':
				debug('Skipping non-PNG icon')
				continue
			source = icon.getAttribute('href')
			if source:
				break
			warn('Missing "href" attribute on <icon> in %s', interface)
		else:
			info('No PNG icons found in %s', interface)
			return

		dl = self.handler.get_download(source, force = force)
		if dl.on_success:
			# Possibly we should handle this better, but it's unlikely anyone will need
			# to use an icon as an interface or implementation as well, and some of the code
			# may assume it's OK keep asking for the same icon to be downloaded.
			info("Already have a handler for %s; not adding another", source)
			return
		dl.on_success.append(lambda stream: self.store_icon(interface, stream))

	def store_icon(self, interface, stream):
		"""Called when an icon has been successfully downloaded.
		Subclasses may wish to wrap this to repaint the display."""
		from zeroinstall.injector import basedir
		import shutil
		icons_cache = basedir.save_cache_path(config_site, 'interface_icons')
		icon_file = file(os.path.join(icons_cache, escape(interface.uri)), 'w')
		shutil.copyfileobj(stream, icon_file)
	
	def get_implementation_path(self, impl):
		assert isinstance(impl, Implementation)
		if impl.id.startswith('/'):
			return impl.id
		return iface_cache.stores.lookup(impl.id)

	def get_implementation(self, interface):
		assert isinstance(interface, Interface)

		if not interface.name and not interface.feeds:
			raise SafeException("We don't have enough information to "
					    "run this program yet. "
					    "Need to download:\n%s" % interface.uri)
		try:
			return self.implementation[interface]
		except KeyError, ex:
			if interface.implementations:
				offline = ""
				if self.network_use == network_offline:
					offline = "\nThis may be because 'Network Use' is set to Off-line."
				raise SafeException("No usable implementation found for '%s'.%s" %
						(interface.name, offline))
			raise ex

	def walk_interfaces(self):
		# Deprecated
		return iter(self.implementation)

	def check_signed_data(self, download, signed_data):
		iface_cache.check_signed_data(download.interface, signed_data, self.handler)
	
	def get_cached(self, impl):
		if impl.id.startswith('/'):
			return os.path.exists(impl.id)
		else:
			try:
				path = self.get_implementation_path(impl)
				assert path
				return True
			except:
				pass # OK
		return False
	
	def add_to_cache(self, source, data):
		iface_cache.add_to_cache(source, data)
	
	def get_uncached_implementations(self):
		uncached = []
		for iface in self.implementation:
			impl = self.implementation[iface]
			assert impl
			if not self.get_cached(impl):
				uncached.append((iface, impl))
		return uncached
	
	def refresh_all(self, force = True):
		for x in self.walk_interfaces():
			self.begin_iface_download(x, force)
			for f in self.usable_feeds(x):
				feed_iface = self.get_interface(f.uri)
				self.begin_iface_download(feed_iface, force)
	
	def interface_changed(self, interface):
		debug("interface_changed(%s): recalculating", interface)
		self.recalculate()
	
	def get_feed_targets(self, feed_iface_uri):
		"""Return a list of Interfaces for which feed_iface can be a feed.
		This is used by --feed. If there are no interfaces, raises SafeException."""
		feed_iface = self.get_interface(feed_iface_uri)
		if not feed_iface.feed_for:
			if not feed_iface.name:
				raise SafeException("Can't get feed targets for '%s'; failed to load interface." %
						feed_iface_uri)
			raise SafeException("Missing <feed-for> element in '%s'; "
					"this interface can't be used as a feed." % feed_iface_uri)
		feed_targets = feed_iface.feed_for
		if not feed_iface.name:
			warn("Warning: unknown interface '%s'" % feed_iface_uri)
		return [self.get_interface(uri) for uri in feed_targets]
	
	def get_icon_path(self, iface):
		"""Get an icon for this interface. If the icon is in the cache, use that.
		If not, start a download. If we already started a download (successful or
		not) do nothing. Returns None if no icon is currently available."""
		path = iface_cache.get_icon_path(iface)
		if path:
			return path

		if self.network_use == network_offline:
			info("No icon present for %s, but off-line so not downloading", iface)
			return None

		self.begin_icon_download(iface)
		return None
	
	def get_best_source(self, impl):
		"""Return the best download source for this implementation."""
		if impl.download_sources:
			return impl.download_sources[0]
		return None
