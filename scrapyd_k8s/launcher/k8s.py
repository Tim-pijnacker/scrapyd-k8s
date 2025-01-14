import kubernetes
import kubernetes.stream

from ..utils import native_stringify_dict

class K8s:

    LABEL_PROJECT = 'org.scrapy.project'
    LABEL_SPIDER = 'org.scrapy.spider'
    LABEL_JOB_ID = 'org.scrapy.job_id'

    # translates status to scrapyd terminology
    STATUS_MAP = {
        'Pending': 'pending',
        'Waiting': 'pending',
        'Running': 'running',
        'Succeeded': 'finished',
        'Completed': 'finished',
        'Terminated': 'finished',
        # Failed
        # Unknown
    }

    def __init__(self, config):
        self._namespace = config.scrapyd().get('namespace', 'default')
        self._pull_secret = config.scrapyd().get('pull_secret')
        # TODO figure out where to put Kubernetes initialisation
        try:
            kubernetes.config.load_incluster_config()
        except kubernetes.config.config_exception.ConfigException:
            kubernetes.config.load_kube_config()
        self._k8s = kubernetes.client.CoreV1Api()
        self._k8s_batch = kubernetes.client.BatchV1Api()

    def listjobs(self, project=None):
        label = self.LABEL_PROJECT + ('=%s'%(project) if project else '')
        jobs = self._k8s_batch.list_namespaced_job(namespace=self._namespace, label_selector=label)
        jobs = [self._parse_job(j) for j in jobs.items]
        return jobs

    def schedule(self, repository, project, version, spider, job_id, env_config, env_secret, settings, args):
        job_name = self._k8s_job_name(project, job_id)
        _settings = [i for k, v in native_stringify_dict(settings, keys_only=False).items() for i in ['-s', f"{k}={v}"]]
        _args = [i for k, v in native_stringify_dict(args, keys_only=False).items() for i in ['-a', f"{k}={v}"]]
        env = {
            'SCRAPY_PROJECT': project,
            'SCRAPYD_SPIDER': spider,
            'SCRAPYD_JOB': job_id,
        }
        labels = {
            self.LABEL_JOB_ID: job_id,
            self.LABEL_PROJECT: project,
            self.LABEL_SPIDER: spider,
        }
        env_from = []
        if env_config:
            env_from.append(kubernetes.client.V1EnvFromSource(
                config_map_ref=kubernetes.client.V1ConfigMapEnvSource(name=env_config, optional=False)
            ))
        if env_secret:
            env_from.append(kubernetes.client.V1EnvFromSource(
                secret_ref=kubernetes.client.V1SecretEnvSource(name=env_secret, optional=False)
            ))
        container = kubernetes.client.V1Container(
            name=job_name,
            image=repository + ':' + version,
            args=['scrapy', 'crawl', spider, *_args, *_settings],
            env=[kubernetes.client.V1EnvVar(k, v) for k, v in env.items()],
            env_from=env_from
        )
        pod_template = kubernetes.client.V1PodTemplateSpec(
            metadata=kubernetes.client.V1ObjectMeta(name=job_name, labels=labels),
            spec=kubernetes.client.V1PodSpec(
                containers=[container],
                restart_policy='Never',
                image_pull_secrets=[kubernetes.client.V1LocalObjectReference(s) for s in [self._pull_secret] if s]
            )
        )
        job_spec = kubernetes.client.V1JobSpec(
            template=pod_template,
            # suspend=True, # TODO implement scheduler with suspend
            completions=1,
            backoff_limit=0 # don't retry (TODO reconsider)
        )
        job = kubernetes.client.V1Job(
            api_version='batch/v1',
            kind='Job',
            metadata=kubernetes.client.V1ObjectMeta(name=job_name, labels=labels),
            spec=job_spec
        )
        r = self._k8s_batch.create_namespaced_job(namespace=self._namespace, body=job)

    def cancel(self, project, job_id, signal):
        job = self._get_job(project, job_id)
        if not job:
            return None

        prevstate = self._k8s_job_to_scrapyd_status(job)
        if prevstate == 'finished':
            pass # nothing to do
        elif prevstate == 'running':
            # kill pod (retry is disabled, so there should be only one pod)
            pod = self._get_pod(project, job_id)
            if not pod:
                # job apparently just ended, fine
                return None
            self._k8s_kill(pod.metadata.name, signal)
        else:
            # not started yet, delete job
            self._k8s_batch.delete_namespaced_job(
                namespace=self._namespace,
                name=job.metadata.name,
                body=kubernetes.client.V1DeleteOptions(
                    propagation_policy='Foreground',
                    grace_period_seconds=0
                )
            )
        return prevstate

    def _parse_job(self, job):
        return {
            'id': job.metadata.labels.get(self.LABEL_JOB_ID),
            'state': self._k8s_job_to_scrapyd_status(job),
            'project': job.metadata.labels.get(self.LABEL_PROJECT),
            'spider': job.metadata.labels.get(self.LABEL_SPIDER)
        }
    
    def _get_job(self, project, job_id):
        label = self.LABEL_JOB_ID + '=' + job_id
        r = self._k8s_batch.list_namespaced_job(namespace=self._namespace, label_selector=label)
        if not r or not r.items:
            return None
        job = r.items[0]

        if job.metadata.labels.get(self.LABEL_PROJECT) != project:
            # TODO log error
            return None

        return job
   
    def _get_pod(self, project, job_id):
        label = self.LABEL_JOB_ID + '=' + job_id
        r = self._k8s.list_namespaced_pod(namespace=self._namespace, label_selector=label)
        if not r or not r.items:
            return None
        pod = r.items[0]

        if pod.metadata.labels.get(self.LABEL_PROJECT) != project:
            # TODO log error
            return None

        return pod

    def _k8s_to_scrapyd_status(self, status):
        return self.STATUS_MAP.get(status, status.lower())
    
    def _k8s_job_to_scrapyd_status(self, job):
        if job.status.ready:
            return 'running'
        elif job.status.succeeded:
            return 'finished'
        else: # including failure modes
            return 'pending'

    def _k8s_job_name(self, project, job_id):
        return '-'.join(('scrapyd', project, job_id))
    
    def _k8s_kill(self, pod_name, signal):
        # exec needs stream, which modified client, so use separate instance
        k8s = kubernetes.client.CoreV1Api()
        resp = kubernetes.stream(
            k8s.connect_get_namespaced_pod_exec,
            pod_name,
            'default',
            # this is a bit blunt, bit it works and is usually available
            command=['/usr/sbin/killall5', '-' + signal]
        )
        # TODO figure out how to get return value
        
