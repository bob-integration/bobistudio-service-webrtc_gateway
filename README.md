# bobistudio-service-webrtc_gateway

Service **WebRTC** de Bobi.Studio : passerelle [MediaMTX](https://github.com/bluenviron/mediamtx)
dédiée, qui reçoit les flux de nos encodeurs et les redistribue au navigateur. Sous-module de
[bobi.studio](https://github.com/bob-integration/bobistudio), monté sur `services/webrtc_gateway/`.

## Ce que ça fait

C'est par elle que passe toute vidéo vue dans un navigateur : le panneau de monitoring présent sur
chaque page, la prévisualisation d'un flux sur la page Streams, la destination `webrtc` d'un flux
sortant.

Un `streamer` pousse en RTSP (ou WHIP) vers la passerelle, qui sert la lecture en **WHEP** et une
page de lecture embarquable. L'orchestrateur ne relaie pas le média : il donne l'adresse, et le flux
va directement de la passerelle au navigateur.

## Un conteneur dédié, pas une bibliothèque

La passerelle est un conteneur managé comme un autre, déployé depuis **Réglages → WebRTC** et non
depuis la palette : c'est de l'**infrastructure partagée**, pas une ressource de projet. Elle
survit donc au rappel d'un projet et au RAZ, qui épargnent ce que l'exploitant ne peut pas recréer
d'un clic.

Elle naît en poids CPU « Arrière-plan » : elle relaie des aperçus, et laisse donc la main à un flux
d'antenne quand le nœud sature. Posé à la création seulement — ensuite, le choix de l'exploitant
est gardé.

## Adresses et ports

Deux parties joignent la passerelle, et pas par le même chemin. L'**orchestrateur** l'appelle depuis
le nœud, sur l'IP du conteneur et des ports qui restent fermés au LAN. Le **navigateur** la joint
depuis le LAN, sur l'adresse de l'hôte, là où les ports publiés aboutissent. Le service rend
l'adresse qui convient à celui qui demande — aucun appelant n'a à connaître la règle.

Une session WebRTC demande deux choses distinctes, d'où quatre ports publiés :

| Port | Ce qui passe |
|---|---|
| WHEP, TCP | la signalisation, et la page de lecture embarquée |
| média, UDP | le flux audio/vidéo lui-même, par ICE |
| média, TCP | même numéro, pour un poste dont le réseau bloque l'UDP sortant |
| ingest RTSP, TCP | un encodeur tiers ; nos propres streamers sont sur le réseau de la passerelle |

Les candidats ICE portent l'adresse de l'hôte vue du site, afin que le navigateur reçoive une
adresse qu'il peut atteindre, et l'IP du conteneur reste candidate : c'est par elle que
l'orchestrateur lit depuis le nœud. En mode « site », la passerelle est une machine du LAN comme
une autre — rien n'est ajouté et rien n'est publié.

Les deux déclarations se tiennent : la garde `verif_webrtc_media_ice` du dépôt principal vérifie que
le port média écrit dans la configuration de MediaMTX figure dans la liste des ports publiés.

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
garde le binaire pré-baké de l'image s'il porte cette version ; sinon il télécharge l'archive
officielle et **vérifie son empreinte SHA-256**, connue pour la version fixée. Changer de version,
c'est donc mettre à jour les deux.

Seuls les protocoles que le produit utilise sont activés — le serveur MoQ, actif par défaut depuis
MediaMTX 1.19, est coupé.

L'horodatage absolu (`useAbsoluteTimestamp`) est activé en ingestion RTSP : la passerelle garde la
date NTP de l'encodeur au lieu de dater les paquets à leur arrivée, et c'est cette date qui cale
son et image. L'image apparaît donc au premier rapport RTCP accepté, environ 5 s après le
**démarrage** d'un encodeur ; un changement de source à chaud du moniteur n'attend pas.

## Installation

Le service se déclare proposable à l'installation (`install_offer`) : proposé et pré-coché à la fin
du parcours, sans prérequis de site — il n'annonce rien sur le réseau, contrairement à NMOS ou SAP.
C'est le service lui-même qui le déclare, et non l'installeur qui tient une liste.

Image Docker `bobi-webrtc` (MediaMTX pré-baké → démarrage immédiat), constructible depuis
**Réglages → Déploiement**.

## Licence

GNU GPL v3 ou ultérieure — voir `LICENSE`.
