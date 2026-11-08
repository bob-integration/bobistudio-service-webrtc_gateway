# bobistudio-service-webrtc_gateway

Service **WebRTC** de Bobi.Studio : passerelle [MediaMTX](https://github.com/bluenviron/mediamtx)
dédiée, qui reçoit les flux de nos encodeurs et les redistribue au navigateur. Sous-module de
[bobi.studio](https://github.com/bob-integration/bobistudio), monté sur `services/webrtc_gateway/`.

## Ce que ça fait

C'est par elle que passe **toute vidéo vue dans un navigateur** : le panneau de monitoring présent
sur chaque page, la prévisualisation d'un flux sur la page Streams, et la destination `webrtc` d'un
flux sortant. Sans elle, l'UDP et le SRT fonctionnent et le navigateur reste noir.

Un `streamer` pousse en RTSP (ou WHIP) vers la passerelle, qui sert la lecture en **WHEP** et une
page de lecture embarquable. L'orchestrateur ne relaie pas le média : il donne l'adresse, le flux
va directement de la passerelle au navigateur.

## Un conteneur dédié, pas une bibliothèque

La passerelle est un conteneur managé comme un autre, déployé depuis **Réglages → WebRTC** et non
depuis la palette : c'est de l'**infrastructure partagée**, pas une ressource de projet. Elle
survit donc au rappel d'un projet et au RAZ, qui ne balaient pas ce que l'exploitant ne peut pas
recréer d'un clic.

Elle naît en poids CPU « Arrière-plan » : elle relaie des aperçus et ne doit pas disputer le CPU à
un flux d'antenne quand le nœud sature. Posé à la création seulement — ensuite, le choix de
l'exploitant est gardé.

## Deux adresses, quatre ports

Le piège de ce service est là, et il a coûté deux pannes dont **tous les voyants étaient verts**.

En mode « réseau interne », l'IP de la passerelle est celle du *conteneur*, privée au nœud. Une URL
bâtie avec elle et envoyée à un navigateur pointe une adresse qui n'existe pas pour lui : lecture
noire, sans erreur et sans alerte. Le service distingue donc explicitement l'adresse jointe par le
**navigateur** (celle de l'hôte, où le port publié aboutit) de celle que l'**orchestrateur** appelle
(celle du conteneur, sur des ports non publiés).

Et la **signalisation n'est pas le média** : WHEP répond sur son port TCP pendant que le flux ICE
passe par un autre, en UDP. Déclarer le premier sans le second donne une session qui s'ouvre, puis
meurt sur `deadline exceeded while waiting connection`. Le port média est donc publié en UDP **et**
en TCP sur le même numéro, pour un poste dont le réseau bloque l'UDP sortant.

Les candidats ICE annoncent en plus l'adresse de l'hôte vue du site. En mode « site », rien n'est
ajouté et rien n'est publié : le conteneur y est une machine du LAN.

Garde : `verif_webrtc_media_ice` du dépôt principal vérifie que le port écrit dans la configuration
de MediaMTX est bien dans la liste des ports publiés. S'ils divergeaient, ICE annoncerait une
adresse où rien n'écoute — exactement le symptôme d'origine.

## Écoute de console en Opus

`console_listen.py` est un second usage, à la demande : un lecteur WHEP sur `cl-<ip>-<bus>` fait
démarrer par MediaMTX **un** ffmpeg par (console, bus), qui lit la sortie L16 de la console et
publie de l'Opus 96 kbit/s (*lowdelay*, trames de 10 ms), arrêté 10 s après le départ du dernier
auditeur. Aucun conteneur, aucun *reaper*, et un seul écouteur de console consommé quel que soit le
nombre d'auditeurs.

Mesuré le 2026-10-16 : 52-57 ms médian de la sortie de la console à l'audio relu, 98 kbit/s, ~6 %
d'un cœur ; démarrage à froid 0,5 s. Le tampon de gigue du navigateur n'est pas dans cette mesure.

## Version de MediaMTX

Elle est **fixée par le service** (`MEDIAMTX_VERSION`), pas réglable depuis l'interface. Le script
ne garde le binaire pré-baké de l'image que s'il porte cette version ; sinon il télécharge
l'archive officielle et **vérifie son empreinte SHA-256**. Une version dont l'empreinte est inconnue
est refusée, plutôt que déployée sans vérification. Changer de version, c'est donc mettre à jour les
deux endroits.

Le serveur MoQ, actif par défaut depuis MediaMTX 1.19, est coupé : il ouvrait deux ports et
fabriquait un certificat auto-signé.

⚠ L'horodatage absolu (`useAbsoluteTimestamp`) est activé en ingestion RTSP — la passerelle garde la
date NTP de l'encodeur au lieu de dater à l'arrivée, et c'est cette date qui cale son et image.
Contrepartie : MediaMTX jette les paquets reçus avant le premier rapport RTCP qu'il accepte, soit
environ 5 s de plus avant l'image au **démarrage** d'un encodeur (pas au changement de source à
chaud du moniteur).

## Installation

Le service se déclare proposable à l'installation (`install_offer`), proposé et pré-coché à la fin
du parcours, sans prérequis de site. C'est délibéré : les services portant une clé `*_enabled` sont
éteints par défaut, et un service éteint n'a pas d'onglet dans Réglages — celui-ci serait donc resté
invisible alors que son absence éteint toute lecture navigateur.

Image Docker `bobi-webrtc` (MediaMTX pré-baké → démarrage immédiat), constructible depuis
**Réglages → Déploiement**.

## Licence

GNU GPL v3 ou ultérieure — voir `LICENSE`.
