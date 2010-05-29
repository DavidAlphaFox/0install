#!/usr/bin/env python
from basetest import BaseTest
from StringIO import StringIO
import sys, os
import unittest

sys.path.insert(0, '..')
from zeroinstall.injector import selections, model, reader, policy, iface_cache, namespaces, qdom

mydir = os.path.dirname(os.path.abspath(__file__))

class TestSelections(BaseTest):
	def testSelections(self):
		p = policy.Policy('http://foo/Source.xml', src = True)
		source = iface_cache.iface_cache.get_interface('http://foo/Source.xml')
		compiler = iface_cache.iface_cache.get_interface('http://foo/Compiler.xml')
		reader.update(source, 'Source.xml')
		reader.update(compiler, 'Compiler.xml')

		p.freshness = 0
		p.network_use = model.network_full
		#import logging
		#logging.getLogger().setLevel(logging.DEBUG)
		p.recalculate()

		def assertSel(s):
			self.assertEquals('http://foo/Source.xml', s.interface)
			self.assertEquals(2, len(s.selections))

			sels = [(sel.interface, sel) for sel in s.selections.values()]
			sels.sort()
			sels = [sel for uri,sel in sels]
			
			self.assertEquals('http://foo/Compiler.xml', sels[0].interface)
			self.assertEquals('http://foo/Source.xml', sels[1].interface)

			self.assertEquals("sha1=345", sels[0].id)
			self.assertEquals("1.0", sels[0].version)

			self.assertEquals('sha1=234', sels[1].id)
			self.assertEquals("1.0", sels[1].version)
			self.assertEquals("bar", sels[1].attrs['http://namespace foo'])
			self.assertEquals("1.0", sels[1].attrs['version'])
			assert 'version-modifier' not in sels[1].attrs

			self.assertEquals(0, len(sels[0].bindings))
			self.assertEquals(0, len(sels[0].dependencies))

			self.assertEquals(1, len(sels[1].bindings))
			self.assertEquals('.', sels[1].bindings[0].insert)

			self.assertEquals(1, len(sels[1].dependencies))
			dep = sels[1].dependencies[0]
			self.assertEquals('http://foo/Compiler.xml', dep.interface)
			self.assertEquals(1, len(dep.bindings))
			self.assertEquals(["sha1=345"], sels[0].digests)

		s1 = selections.Selections(p)
		s1.selections['http://foo/Source.xml'].attrs['http://namespace foo'] = 'bar'
		assertSel(s1)

		xml = s1.toDOM().toxml("utf-8")
		root = qdom.parse(StringIO(xml))
		self.assertEquals(namespaces.XMLNS_IFACE, root.uri)

		s2 = selections.Selections(root)
		assertSel(s2)
	
	def testLocalPath(self):
		iface = os.path.join(mydir, "Local.xml")
		p = policy.Policy(iface)
		p.recalculate()
		s1 = selections.Selections(p)
		xml = s1.toDOM().toxml("utf-8")
		root = qdom.parse(StringIO(xml))
		s2 = selections.Selections(root)
		local_path = s2.selections[iface].local_path
		assert os.path.isdir(local_path), local_path
		assert not s2.selections[iface].digests, s2.selections[iface].digests

if __name__ == '__main__':
	unittest.main()
