from setuptools import setup, find_packages

setup(name='juju-okeanos',
      version="0.1.0",
      classifiers=[
          'Intended Audience :: Developers',
          'Programming Language :: Python',
          'Operating System :: OS Independent'],
      author='Konstantinos Tsakalozos',
      author_email='tsakas@gmail.com',
      description="Okeanos integration with juju",
      long_description=open("README.rst").read(),
      url='https://github.com/ktsakalozos/juju-okeanos',
      license='BSD',
      packages=find_packages(),
      install_requires=["PyYAML", "requests", "jujuclient", "kamaki"],
      tests_require=["nose", "mock"],
      entry_points={
          "console_scripts": [
              'juju-okeanos = juju_okeanos.cli:main']},
      )
